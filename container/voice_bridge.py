#!/usr/bin/env python3
"""Pont Wyoming -> Jeedom, sans Home Assistant.

Le pont joue le rôle que Home Assistant tient d'habitude face à wyoming-satellite :
il se connecte à chaque satellite (port 10700), lui demande de streamer le micro,
puis enchaîne :

    wake word (si le satellite ne le fait pas lui-même)
      -> STT Whisper (wyoming-faster-whisper)
      -> moteur d'interaction Jeedom (JSON-RPC interact::tryToReply)
      -> réponse vocale Piper (optionnelle)

Les deux modes de wyoming-satellite sont gérés sans réglage côté pont :
  * satellite lancé SANS --wake-uri : il streame en continu et le pont détecte le
    wake word via wyoming-openwakeword (section [wake] obligatoire) ;
  * satellite lancé AVEC --wake-uri : il détecte le wake word localement et
    n'envoie l'audio qu'après détection (section [wake] inutile).

Tout reste sur le réseau local : aucun service tiers n'est appelé.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import re
import ssl
import sys
import tomllib
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import webrtcvad
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.ping import Ping, Pong
from wyoming.pipeline import PipelineStage, RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.snd import Played
from wyoming.tts import Synthesize, SynthesizeVoice
from wyoming.vad import VoiceStarted, VoiceStopped
from wyoming.wake import Detect, Detection

_LOGGER = logging.getLogger("voice_bridge")

# Format audio utilisé pour le wake word, le VAD et Whisper.
RATE, WIDTH, CHANNELS = 16000, 2, 1

# Hallucinations classiques de Whisper en français sur du bruit ou du silence.
DEFAULT_IGNORED_PHRASES = [
    "sous-titres realises par la communaute d'amara.org",
    "sous-titrage st' 501",
    "sous-titrage societe radio-canada",
    "merci d'avoir regarde",
    "abonnez-vous",
]


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


@dataclass
class VadConfig:
    aggressiveness: int = 2  # 0 (tolérant) à 3 (strict) pour webrtcvad
    speech_seconds: float = 0.3  # parole cumulée avant de considérer que la commande a commencé
    silence_seconds: float = 0.8  # silence qui termine la commande
    no_speech_timeout: float = 6.0  # abandon si personne ne parle après le wake word
    max_command_seconds: float = 15.0  # durée maximale d'une commande


@dataclass
class SatelliteConfig:
    name: str
    uri: str
    profile: Optional[str] = None  # profil Jeedom transmis au moteur d'interaction


@dataclass
class Config:
    satellites: list[SatelliteConfig]
    asr_uri: str
    jeedom_url: str
    jeedom_apikey: str
    language: str = "fr"
    ignored_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORED_PHRASES))
    lowercase: bool = True
    stt_timeout: float = 30.0
    wake_uri: Optional[str] = None
    wake_names: list[str] = field(default_factory=lambda: ["okay_nabu"])
    tts_uri: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_timeout: float = 30.0
    jeedom_verify_tls: bool = True
    jeedom_timeout: float = 10.0
    jeedom_reply_cmd: Optional[int] = None
    vad: VadConfig = field(default_factory=VadConfig)
    ping_interval: float = 5.0
    reconnect_seconds: float = 5.0
    audio_timeout: float = 5.0


def load_config(path: Path) -> Config:
    with path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    stt = raw.get("stt", {})
    wake = raw.get("wake", {})
    tts = raw.get("tts", {})
    jeedom = raw.get("jeedom", {})
    bridge = raw.get("bridge", {})

    satellites = [
        SatelliteConfig(name=s["name"], uri=s["uri"], profile=s.get("profile"))
        for s in raw.get("satellites", [])
    ]
    if not satellites:
        raise ValueError("Aucun satellite déclaré ([[satellites]])")
    if "uri" not in stt:
        raise ValueError("[stt] uri est obligatoire")
    if ("url" not in jeedom) or ("apikey" not in jeedom):
        raise ValueError("[jeedom] url et apikey sont obligatoires")

    return Config(
        satellites=satellites,
        asr_uri=stt["uri"],
        language=stt.get("language", "fr"),
        ignored_phrases=stt.get("ignored_phrases", list(DEFAULT_IGNORED_PHRASES)),
        lowercase=stt.get("lowercase", True),
        stt_timeout=stt.get("timeout", 30.0),
        wake_uri=wake.get("uri"),
        wake_names=wake.get("names", ["okay_nabu"]),
        tts_uri=tts.get("uri"),
        tts_voice=tts.get("voice"),
        tts_timeout=tts.get("timeout", 30.0),
        jeedom_url=jeedom["url"],
        jeedom_apikey=jeedom["apikey"],
        jeedom_verify_tls=jeedom.get("verify_tls", True),
        jeedom_timeout=jeedom.get("timeout", 10.0),
        jeedom_reply_cmd=jeedom.get("reply_cmd"),
        vad=VadConfig(**raw.get("vad", {})),
        ping_interval=bridge.get("ping_interval", 5.0),
        reconnect_seconds=bridge.get("reconnect_seconds", 5.0),
        audio_timeout=bridge.get("audio_timeout", 5.0),
    )


# -----------------------------------------------------------------------------
# Détection de fin de commande (VAD)
# -----------------------------------------------------------------------------


class CommandSegmenter:
    """Découpe une commande vocale avec webrtcvad (trames de 30 ms à 16 kHz)."""

    FRAME_SECONDS = 0.03
    FRAME_BYTES = int(RATE * FRAME_SECONDS) * WIDTH

    def __init__(self, cfg: VadConfig) -> None:
        self.cfg = cfg
        self._vad = webrtcvad.Vad(cfg.aggressiveness)
        self._buffer = b""
        self._elapsed = 0.0
        self._speech_run = 0.0
        self._silence_run = 0.0
        self.speech_started = False
        self.finished = False

    def process(self, audio: bytes) -> None:
        """Traite de l'audio 16 kHz / 16 bits / mono."""
        self._buffer += audio
        while (len(self._buffer) >= self.FRAME_BYTES) and (not self.finished):
            frame = self._buffer[: self.FRAME_BYTES]
            self._buffer = self._buffer[self.FRAME_BYTES :]
            self._elapsed += self.FRAME_SECONDS
            is_speech = self._vad.is_speech(frame, RATE)

            if not self.speech_started:
                if is_speech:
                    self._speech_run += self.FRAME_SECONDS
                    if self._speech_run >= self.cfg.speech_seconds:
                        self.speech_started = True
                else:
                    # Décroissance : quelques trames bruitées isolées ne suffisent pas
                    self._speech_run = max(0.0, self._speech_run - self.FRAME_SECONDS)
                    if self._elapsed >= self.cfg.no_speech_timeout:
                        self.finished = True
            elif is_speech:
                self._silence_run = 0.0
            else:
                self._silence_run += self.FRAME_SECONDS
                if self._silence_run >= self.cfg.silence_seconds:
                    self.finished = True

            if self._elapsed >= self.cfg.max_command_seconds:
                self.finished = True


# -----------------------------------------------------------------------------
# Jeedom
# -----------------------------------------------------------------------------


class JeedomError(Exception):
    pass


class JeedomClient:
    """Client JSON-RPC du moteur d'interaction Jeedom (méthode interact::tryToReply)."""

    def __init__(self, cfg: Config) -> None:
        self.url = cfg.jeedom_url
        self.apikey = cfg.jeedom_apikey
        self.timeout = cfg.jeedom_timeout
        self.reply_cmd = cfg.jeedom_reply_cmd
        self._ids = itertools.count(1)
        self._ssl: Optional[ssl.SSLContext] = None
        if self.url.startswith("https") and not cfg.jeedom_verify_tls:
            self._ssl = ssl.create_default_context()
            self._ssl.check_hostname = False
            self._ssl.verify_mode = ssl.CERT_NONE

    def ask(self, query: str, profile: Optional[str] = None) -> str:
        """Envoie une phrase au moteur d'interaction et renvoie la réponse texte.

        Appel bloquant : à lancer via asyncio.to_thread. La clé API part dans le corps
        de la requête POST (et non dans l'URL) pour ne pas finir dans les logs du
        serveur web.
        """
        params: dict = {"apikey": self.apikey, "query": query}
        if profile:
            params["profile"] = profile
        if self.reply_cmd:
            params["reply_cmd"] = self.reply_cmd

        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "interact::tryToReply",
            "params": params,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        if body.get("error"):
            error = body["error"]
            raise JeedomError(f"{error.get('message')} (code {error.get('code')})")

        result = body.get("result")
        if isinstance(result, dict):
            return str(result.get("reply") or "")
        return str(result or "")


# -----------------------------------------------------------------------------
# Session satellite
# -----------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : sert à comparer des phrases."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w]+", " ", text)
    return text.strip()


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def describe_error(err: BaseException) -> str:
    return str(err) or type(err).__name__


class SatelliteSession:
    """Connexion persistante à un satellite et exécution de ses pipelines."""

    def __init__(self, cfg: Config, sat: SatelliteConfig, jeedom: JeedomClient) -> None:
        self.cfg = cfg
        self.sat = sat
        self.jeedom = jeedom
        self.log = logging.getLogger(f"voice_bridge.{sat.name}")
        self._client: Optional[AsyncClient] = None
        self._write_lock = asyncio.Lock()
        self._audio: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=1000)
        self._accept_audio = False
        self._pipeline_task: Optional[asyncio.Task] = None
        self._played = asyncio.Event()
        self._ignored = [normalize(p) for p in cfg.ignored_phrases]

    # --- connexion -----------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - on reconnecte quoi qu'il arrive
                self.log.warning("Satellite %s indisponible : %s", self.sat.uri, describe_error(err))
            await asyncio.sleep(self.cfg.reconnect_seconds)

    async def _run_connection(self) -> None:
        client = AsyncClient.from_uri(self.sat.uri)
        await asyncio.wait_for(client.connect(), timeout=10)
        self._client = client
        self.log.info("Connecté au satellite %s", self.sat.uri)
        ping_task: Optional[asyncio.Task] = None
        try:
            await self._send(Describe().event())
            # Le satellite commence à streamer (ou attend son wake word local)
            await self._send(RunSatellite().event())
            # Les pings activent aussi le keep-alive côté satellite (réponse attendue en 5 s)
            ping_task = asyncio.create_task(self._ping_loop())
            while True:
                event = await asyncio.wait_for(
                    client.read_event(), timeout=self.cfg.ping_interval * 3
                )
                if event is None:
                    raise ConnectionError("le satellite a fermé la connexion")
                await self._on_event(event)
        finally:
            if ping_task is not None:
                ping_task.cancel()
            await self._stop_pipeline()
            self._client = None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.ping_interval)
            await self._send(Ping().event())

    async def _send(self, event: Event) -> None:
        client = self._client
        if client is None:
            raise ConnectionError("satellite déconnecté")
        async with self._write_lock:
            await client.write_event(event)

    async def _on_event(self, event: Event) -> None:
        """Ne doit jamais bloquer : sinon les pongs partent en retard et le satellite coupe."""
        if AudioChunk.is_type(event.type):
            if self._accept_audio:
                try:
                    self._audio.put_nowait(AudioChunk.from_event(event))
                except asyncio.QueueFull:
                    pass
        elif Ping.is_type(event.type):
            await self._send(Pong(text=Ping.from_event(event).text).event())
        elif Pong.is_type(event.type):
            pass
        elif RunPipeline.is_type(event.type):
            run_pipeline = RunPipeline.from_event(event)
            self.log.debug("run-pipeline reçu : %s", event.data)
            await self._stop_pipeline()
            # Accepter l'audio dès maintenant : les premiers morceaux de la commande
            # suivent immédiatement run-pipeline quand le wake word est local.
            self._flush_audio()
            self._accept_audio = True
            self._pipeline_task = asyncio.create_task(self._run_pipeline(run_pipeline))
        elif Detection.is_type(event.type):
            self.log.info("Wake word détecté par le satellite : %s", Detection.from_event(event).name)
        elif Played.is_type(event.type):
            self._played.set()
        elif Info.is_type(event.type):
            satellite = event.data.get("satellite") or {}
            self.log.info(
                "Satellite « %s » (wyoming-satellite %s)",
                satellite.get("name", "?"),
                satellite.get("version", "?"),
            )
        else:
            self.log.debug("Événement ignoré : %s", event.type)

    # --- pipeline ------------------------------------------------------------

    async def _stop_pipeline(self) -> None:
        self._accept_audio = False
        task, self._pipeline_task = self._pipeline_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _flush_audio(self) -> None:
        while not self._audio.empty():
            self._audio.get_nowait()

    async def _next_chunk(self, timeout: Optional[float]) -> AudioChunk:
        try:
            return await asyncio.wait_for(self._audio.get(), timeout=timeout)
        except asyncio.TimeoutError as err:
            raise TimeoutError("le satellite n'envoie plus d'audio") from err

    async def _run_pipeline(self, run_pipeline: RunPipeline) -> None:
        stage = run_pipeline.start_stage
        while True:
            detected = stage != PipelineStage.WAKE
            try:
                if stage == PipelineStage.WAKE:
                    await self._wait_wake_word()
                    detected = True
                elif stage != PipelineStage.ASR:
                    self.log.warning("Étape de départ non gérée : %s", stage.value)
                    return
                await self._listen_and_handle(run_pipeline.end_stage)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.log.error("Échec du pipeline : %s", describe_error(err))
                if detected:
                    # Un satellite à wake word local attend transcript ou error pour
                    # arrêter de streamer : on lui envoie toujours l'un des deux.
                    try:
                        await self._send(Error(text=describe_error(err), code="pipeline-error").event())
                    except Exception:  # noqa: BLE001
                        pass
                if run_pipeline.restart_on_end:
                    await asyncio.sleep(2)
            finally:
                self._accept_audio = False

            if not run_pipeline.restart_on_end:
                return
            stage = PipelineStage.WAKE

    async def _wait_wake_word(self) -> None:
        """Détection distante du wake word (satellite en streaming continu)."""
        if not self.cfg.wake_uri:
            raise RuntimeError(
                "le satellite n'a pas de wake word local et [wake] uri n'est pas configuré"
            )

        self._flush_audio()
        self._accept_audio = True
        wake = AsyncClient.from_uri(self.cfg.wake_uri)
        await asyncio.wait_for(wake.connect(), timeout=10)
        try:
            await wake.write_event(Detect(names=self.cfg.wake_names or None).event())
            await wake.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
            self.log.debug("En attente du wake word %s", self.cfg.wake_names)
            detection_task = asyncio.create_task(self._read_detection(wake))
            try:
                while not detection_task.done():
                    chunk = await self._next_chunk(timeout=None)
                    await wake.write_event(chunk.event())
                detection = detection_task.result()
            finally:
                detection_task.cancel()
        finally:
            try:
                await wake.disconnect()
            except Exception:  # noqa: BLE001
                pass

        self.log.info("Wake word détecté : %s", detection.name)
        # Le satellite joue awake.wav et allume ses LED à la réception de detection
        await self._send(Detection(name=detection.name, timestamp=detection.timestamp).event())

    @staticmethod
    async def _read_detection(wake: AsyncClient) -> Detection:
        while True:
            event = await wake.read_event()
            if event is None:
                raise ConnectionError("le service de wake word a fermé la connexion")
            if Detection.is_type(event.type):
                return Detection.from_event(event)

    async def _listen_and_handle(self, end_stage: PipelineStage) -> None:
        text = await self._transcribe()
        if not text:
            await self._send(
                Error(text="Aucune commande comprise", code="stt-no-text-recognized").event()
            )
            return

        self.log.info("Commande : « %s »", text)
        await self._send(Transcript(text=text).event())
        if end_stage not in (PipelineStage.INTENT, PipelineStage.HANDLE, PipelineStage.TTS):
            return

        try:
            reply = await asyncio.to_thread(self.jeedom.ask, text, self.sat.profile)
        except Exception as err:  # noqa: BLE001
            self.log.error("Jeedom n'a pas répondu : %s", err)
            return

        reply = strip_html(reply)
        self.log.info("Réponse Jeedom : « %s »", reply)
        if reply and (end_stage == PipelineStage.TTS) and self.cfg.tts_uri:
            await self._speak(reply)

    async def _transcribe(self) -> Optional[str]:
        """Envoie l'audio à Whisper jusqu'à la fin de la commande détectée par le VAD."""
        # Pas de vidage de la file : l'audio reçu juste après le wake word est la commande
        self._accept_audio = True
        segmenter = CommandSegmenter(self.cfg.vad)
        converter = AudioChunkConverter(rate=RATE, width=WIDTH, channels=CHANNELS)
        voice_started = False

        asr = AsyncClient.from_uri(self.cfg.asr_uri)
        await asyncio.wait_for(asr.connect(), timeout=10)
        try:
            await asr.write_event(Transcribe(language=self.cfg.language).event())
            await asr.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
            while not segmenter.finished:
                chunk = converter.convert(await self._next_chunk(self.cfg.audio_timeout))
                await asr.write_event(chunk.event())
                segmenter.process(chunk.audio)
                if segmenter.speech_started and not voice_started:
                    voice_started = True
                    await self._send(VoiceStarted().event())

            self._accept_audio = False
            if not voice_started:
                self.log.info("Aucune parole après le wake word")
                return None

            await self._send(VoiceStopped().event())
            await asr.write_event(AudioStop().event())
            text = await asyncio.wait_for(self._read_transcript(asr), timeout=self.cfg.stt_timeout)
        finally:
            try:
                await asr.disconnect()
            except Exception:  # noqa: BLE001
                pass

        return self._clean_transcript(text)

    @staticmethod
    async def _read_transcript(asr: AsyncClient) -> str:
        while True:
            event = await asr.read_event()
            if event is None:
                raise ConnectionError("le service STT a fermé la connexion sans transcription")
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text

    def _clean_transcript(self, text: str) -> Optional[str]:
        text = (text or "").strip()
        normalized = normalize(text)
        if len(normalized) < 2:
            return None
        if any(phrase and phrase in normalized for phrase in self._ignored):
            self.log.info("Transcription ignorée (hallucination connue) : « %s »", text)
            return None
        text = text.rstrip(" .!?…")
        return text.lower() if self.cfg.lowercase else text

    async def _speak(self, text: str) -> None:
        """Synthèse Piper et lecture sur le haut-parleur du satellite."""
        voice = SynthesizeVoice(name=self.cfg.tts_voice) if self.cfg.tts_voice else None
        audio_seconds = 0.0
        self._played.clear()

        tts = AsyncClient.from_uri(self.cfg.tts_uri)
        await asyncio.wait_for(tts.connect(), timeout=10)
        try:
            await tts.write_event(Synthesize(text=text, voice=voice).event())
            await self._send(Synthesize(text=text, voice=voice).event())
            while True:
                event = await asyncio.wait_for(tts.read_event(), timeout=self.cfg.tts_timeout)
                if event is None:
                    raise ConnectionError("le service TTS a fermé la connexion")
                if AudioChunk.is_type(event.type):
                    audio_seconds += AudioChunk.from_event(event).seconds
                    await self._send(event)
                elif AudioStart.is_type(event.type):
                    await self._send(event)
                elif AudioStop.is_type(event.type):
                    await self._send(event)
                    break
        finally:
            try:
                await tts.disconnect()
            except Exception:  # noqa: BLE001
                pass

        # wyoming-satellite >= 1.4.1 envoie « played » en fin de lecture : on attend
        # avant de réécouter, pour ne pas analyser la réponse vocale elle-même.
        try:
            await asyncio.wait_for(self._played.wait(), timeout=audio_seconds + 3)
        except asyncio.TimeoutError:
            pass


# -----------------------------------------------------------------------------
# Outils de diagnostic
# -----------------------------------------------------------------------------


async def describe_service(label: str, uri: str) -> bool:
    try:
        client = AsyncClient.from_uri(uri)
        await asyncio.wait_for(client.connect(), timeout=5)
        try:
            await client.write_event(Describe().event())
            while True:
                event = await asyncio.wait_for(client.read_event(), timeout=5)
                if event is None:
                    raise ConnectionError("connexion fermée")
                if Info.is_type(event.type):
                    break
        finally:
            await client.disconnect()
    except Exception as err:  # noqa: BLE001
        print(f"[KO] {label:9} {uri} : {describe_error(err)}")
        return False

    programs = []
    for kind in ("asr", "wake", "tts", "satellite"):
        value = event.data.get(kind)
        if isinstance(value, dict):
            value = [value]
        for program in value or []:
            models = [m.get("name") for m in program.get("models", []) or program.get("voices", [])]
            shown = ", ".join(models[:5]) + (" …" if len(models) > 5 else "")
            programs.append(f"{program.get('name')} {program.get('version') or ''} [{shown}]")
    print(f"[OK] {label:9} {uri} : {'; '.join(programs) or event.data}")
    return True


async def check(cfg: Config) -> bool:
    ok = await describe_service("stt", cfg.asr_uri)
    if cfg.wake_uri:
        ok &= await describe_service("wake", cfg.wake_uri)
    if cfg.tts_uri:
        ok &= await describe_service("tts", cfg.tts_uri)
    for sat in cfg.satellites:
        ok &= await describe_service(f"sat:{sat.name}", sat.uri)
    try:
        # Requête vide : valide la clé API et l'accès JSON-RPC sans rien déclencher
        await asyncio.to_thread(JeedomClient(cfg).ask, "")
        print(f"[OK] jeedom    {cfg.jeedom_url}")
    except Exception as err:  # noqa: BLE001
        print(f"[KO] jeedom    {cfg.jeedom_url} : {err}")
        ok = False
    return ok


async def run(cfg: Config) -> None:
    jeedom = JeedomClient(cfg)
    sessions = [SatelliteSession(cfg, sat, jeedom) for sat in cfg.satellites]
    await asyncio.gather(*(session.run_forever() for session in sessions))


def main() -> None:
    parser = argparse.ArgumentParser(description="Pont Wyoming -> Jeedom (sans Home Assistant)")
    parser.add_argument("--config", default="/etc/voice-bridge/config.toml")
    parser.add_argument("--check", action="store_true", help="Teste les services et Jeedom puis quitte")
    parser.add_argument("--ask", metavar="TEXTE", help="Envoie TEXTE au moteur d'interaction Jeedom puis quitte")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(Path(args.config))

    if args.check:
        sys.exit(0 if asyncio.run(check(cfg)) else 1)
    if args.ask:
        print(JeedomClient(cfg).ask(args.ask))
        return

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
