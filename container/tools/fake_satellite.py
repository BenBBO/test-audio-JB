#!/usr/bin/env python3
"""Faux satellite Wyoming : rejoue un fichier WAV comme si on parlait au micro.

Sert à tester la chaîne du container (pont + Whisper + Jeedom + Piper) sans le
Raspberry Pi. Déclarer ce faux satellite dans config.toml (uri tcp://127.0.0.1:10799
par exemple), lancer ce script, puis le pont.

Par défaut, simule un satellite à wake word local : il envoie detection puis
run-pipeline (début à l'étape asr) et rejoue le WAV, qui ne doit contenir que la
commande (« allume la lumière du salon »).

Avec --remote-wake, simule un satellite en streaming continu : le pont doit alors
détecter le wake word lui-même, donc le WAV doit contenir le wake word puis la commande.

La réponse vocale éventuelle est enregistrée avec --tts-output reponse.wav.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import wave
from functools import partial
from typing import Optional

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioChunkConverter, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe
from wyoming.ping import Ping, Pong
from wyoming.pipeline import PipelineStage, RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.snd import Played
from wyoming.tts import Synthesize
from wyoming.vad import VoiceStarted, VoiceStopped
from wyoming.wake import Detection

_LOGGER = logging.getLogger("fake_satellite")
RATE, WIDTH, CHANNELS, SAMPLES_PER_CHUNK = 16000, 2, 1, 1024


def load_chunks(wav_path: str) -> list[AudioChunk]:
    with wave.open(wav_path, "rb") as wav_file:
        source = AudioChunk(
            rate=wav_file.getframerate(),
            width=wav_file.getsampwidth(),
            channels=wav_file.getnchannels(),
            audio=wav_file.readframes(wav_file.getnframes()),
        )
    audio = AudioChunkConverter(rate=RATE, width=WIDTH, channels=CHANNELS).convert(source).audio
    size = SAMPLES_PER_CHUNK * WIDTH
    return [
        AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=audio[i : i + size])
        for i in range(0, len(audio), size)
    ]


class Result:
    def __init__(self) -> None:
        self.finished = asyncio.Event()
        self.transcript: Optional[str] = None
        self.error: Optional[str] = None


class FakeSatelliteHandler(AsyncEventHandler):
    def __init__(self, args, chunks, result: Result, *handler_args, **handler_kwargs) -> None:
        super().__init__(*handler_args, **handler_kwargs)
        self.args = args
        self.chunks = chunks
        self.result = result
        self.stream_task: Optional[asyncio.Task] = None
        self.streaming = False
        self.tts_audio = bytearray()
        self.tts_format: Optional[AudioStart] = None
        self.tts_done = asyncio.Event()

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(
                Event(type="info", data={"satellite": {"name": "faux-satellite", "version": "test"}})
            )
        elif Ping.is_type(event.type):
            await self.write_event(Pong(text=Ping.from_event(event).text).event())
        elif RunSatellite.is_type(event.type):
            _LOGGER.info("run-satellite reçu : début du streaming")
            if self.stream_task is None:
                self.stream_task = asyncio.create_task(self._stream())
        elif Detection.is_type(event.type):
            _LOGGER.info("detection reçue : %s", Detection.from_event(event).name)
        elif VoiceStarted.is_type(event.type):
            _LOGGER.info("voice-started reçu")
        elif VoiceStopped.is_type(event.type):
            _LOGGER.info("voice-stopped reçu")
        elif Transcript.is_type(event.type):
            self.result.transcript = Transcript.from_event(event).text
            _LOGGER.info("transcript reçu : « %s »", self.result.transcript)
            if not self.args.remote_wake:
                self.streaming = False  # comme wyoming-satellite à wake word local
        elif Error.is_type(event.type):
            self.result.error = Error.from_event(event).text
            _LOGGER.info("error reçu : %s", self.result.error)
            self.streaming = self.args.remote_wake
            self.result.finished.set()
        elif Synthesize.is_type(event.type):
            _LOGGER.info("synthesize reçu : « %s »", Synthesize.from_event(event).text)
        elif AudioStart.is_type(event.type):
            self.tts_format = AudioStart.from_event(event)
            self.tts_audio = bytearray()
        elif AudioChunk.is_type(event.type):
            self.tts_audio.extend(AudioChunk.from_event(event).audio)
        elif AudioStop.is_type(event.type):
            self._save_tts()
            await self.write_event(Played().event())
            self.tts_done.set()
        else:
            _LOGGER.debug("événement %s", event.type)
        return True

    def _save_tts(self) -> None:
        fmt = self.tts_format
        seconds = len(self.tts_audio) / (fmt.rate * fmt.width * fmt.channels) if fmt else 0
        _LOGGER.info("réponse vocale reçue : %.1f s", seconds)
        if self.args.tts_output and fmt:
            with wave.open(self.args.tts_output, "wb") as wav_file:
                wav_file.setframerate(fmt.rate)
                wav_file.setsampwidth(fmt.width)
                wav_file.setnchannels(fmt.channels)
                wav_file.writeframes(bytes(self.tts_audio))
            _LOGGER.info("réponse enregistrée dans %s", self.args.tts_output)

    async def _stream(self) -> None:
        await asyncio.sleep(0.5)
        self.streaming = True
        if self.args.remote_wake:
            await self.write_event(
                RunPipeline(
                    start_stage=PipelineStage.WAKE, end_stage=PipelineStage.TTS, restart_on_end=True
                ).event()
            )
        else:
            await self.write_event(Detection(name="faux_wake_word").event())
            await self.write_event(
                RunPipeline(start_stage=PipelineStage.ASR, end_stage=PipelineStage.TTS).event()
            )

        silence = AudioChunk(
            rate=RATE, width=WIDTH, channels=CHANNELS, audio=bytes(SAMPLES_PER_CHUNK * WIDTH)
        )
        for chunk in self.chunks:
            if not self.streaming:
                break
            await self.write_event(chunk.event())
            await asyncio.sleep(chunk.seconds)

        # Silence ensuite, comme un micro dans une pièce calme
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.args.reply_timeout
        while (self.result.transcript is None) and (self.result.error is None):
            if loop.time() > deadline:
                break
            if self.streaming:
                await self.write_event(silence.event())
            await asyncio.sleep(silence.seconds)

        if self.result.transcript is not None:
            # Attente de la réponse vocale éventuelle (en continuant de streamer si besoin)
            while not self.tts_done.is_set() and loop.time() < deadline:
                if self.streaming:
                    await self.write_event(silence.event())
                await asyncio.sleep(silence.seconds)
        self.result.finished.set()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("wav", help="WAV à rejouer (n'importe quel format PCM)")
    parser.add_argument("--uri", default="tcp://0.0.0.0:10799")
    parser.add_argument("--remote-wake", action="store_true", help="Wake word détecté par le pont")
    parser.add_argument("--tts-output", help="Enregistre la réponse vocale dans ce WAV")
    parser.add_argument("--reply-timeout", type=float, default=30.0)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, format="%(asctime)s %(message)s")

    chunks = load_chunks(args.wav)
    result = Result()
    server = AsyncServer.from_uri(args.uri)
    _LOGGER.info("Faux satellite en écoute sur %s (%d morceaux audio)", args.uri, len(chunks))
    server_task = asyncio.create_task(server.run(partial(FakeSatelliteHandler, args, chunks, result)))
    await result.finished.wait()
    server_task.cancel()

    if result.transcript is None:
        print(f"ÉCHEC : pas de transcription ({result.error or 'délai dépassé'})")
        sys.exit(1)
    print(f"OK : transcription « {result.transcript} »")


if __name__ == "__main__":
    asyncio.run(main())
