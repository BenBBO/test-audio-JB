# Assistant vocal local pour Jeedom (sans Home Assistant)

Raspberry Pi 3 + ReSpeaker 2-Mic → container LXC (wake word, Whisper, Piper) → moteur d'interaction Jeedom.
Tout reste sur le réseau local ; aucun service tiers n'est appelé.

```
 Raspberry Pi 3 (Debian 12)                     Container LXC
 ┌───────────────────────────┐                 ┌──────────────────────────────────────────┐
 │ arecord/aplay (ReSpeaker) │                 │ voice_bridge.py  (le chef d'orchestre)   │
 │ wyoming-satellite :10700  │◄── TCP ─────────┤   ├─ wyoming-openwakeword :10400 (mode A)│
 │ [openWakeWord :10400]     │  (le pont se    │   ├─ wyoming-faster-whisper :10300       │
 │  (mode B seulement)       │   connecte au   │   ├─ wyoming-piper :10200 (option)       │
 │ [LED 2-Mic :10500]        │   satellite)    │   └─ HTTP JSON-RPC ──► Jeedom            │
 └───────────────────────────┘                 └──────────────────────────────────────────┘
```

## 1. Ce qui manque dans l'installation actuelle

`wyoming-satellite` est un **serveur** : il attend qu'un « contrôleur » se connecte à lui, lui
demande de streamer, fasse tourner le pipeline (wake word → STT → intention → réponse) et lui
renvoie les événements (détection, transcription, audio de réponse). Ce contrôleur, c'est
normalement Home Assistant. Le serveur Wyoming de Whisper, lui, se contente de transcrire
l'audio qu'on lui envoie : rien ne relie aujourd'hui le satellite à Whisper, ni Whisper à Jeedom.

`container/voice_bridge.py` remplace Home Assistant dans ce rôle, et seulement dans ce rôle :

1. se connecte à chaque satellite et lui envoie `run-satellite` ;
2. détecte le wake word si le satellite ne le fait pas (via wyoming-openwakeword) ;
3. envoie l'audio à Whisper et détecte la fin de la phrase (VAD webrtc) ;
4. transmet la phrase au moteur d'interaction Jeedom (`interact::tryToReply`, JSON-RPC) ;
5. fait lire la réponse de Jeedom par Piper sur le haut-parleur du satellite (optionnel) ;
6. renvoie au satellite `detection`, `voice-started`, `transcript`… pour les sons et les LED.

Le pont gère plusieurs satellites, se reconnecte seul, et maintient un keep-alive ping/pong.

## 2. Architecture : mode A (demandé) ou mode B (recommandé)

| | Mode A : wake word sur le container | Mode B : wake word sur le Pi |
|---|---|---|
| Audio sur le réseau | **en permanence**, 24 h/24 (≈ 256 kbit/s par satellite) | seulement après le wake word |
| Confidentialité | toute personne sur le LAN qui atteint le port 10700 ou capte le trafic entend la pièce en continu (Wyoming n'a ni authentification ni chiffrement) | rien ne sort du Pi avant le wake word |
| Réactivité | dépend du Wi-Fi et du container | son « je t'écoute » immédiat |
| Charge | un détecteur par satellite sur le container | détection sur le CPU du Pi, le container ne fait que Whisper |
| Prérequis | aucun | OS 64 bits sur le Pi (`uname -m` → `aarch64`) |

Les deux respectent vos contraintes (pas de Home Assistant, rien hors du réseau local). Je
recommande le **mode B** : le Pi 3 a la même classe de processeur que le Pi Zero 2 W, pour
lequel la détection locale est prévue d'origine, et c'est le seul mode où l'audio de la pièce
ne circule pas en continu. Passer de l'un à l'autre ne touche que la ligne `ExecStart` du
satellite : le pont s'adapte tout seul, car le satellite lui annonce l'étape de départ
(`wake` en mode A, `asr` en mode B).

À savoir : `wyoming-satellite` n'est plus maintenu (remplacé par *Linux Voice Assistant*, qui
utilise le protocole ESPHome et vise Home Assistant). La version 1.4.1 reste fonctionnelle et
le protocole Wyoming est stable ; on la fige par son tag. Le pont ne dépend que du protocole :
on pourra changer de satellite plus tard sans le réécrire.

## 3. Raspberry Pi

Les commandes supposent l'utilisateur `pi` et l'archive décompressée dans `~/jeedom-voice` :
adapter les chemins et `User=` des fichiers `pi/systemd/*.service` si besoin.

```bash
scp jeedom-voice.tar.gz pi@IP_DU_PI:~      # depuis le poste où se trouve l'archive
tar xzf jeedom-voice.tar.gz                # sur le Pi
```

### 3.1 Vérifier l'audio

```bash
uname -m
arecord -L | grep -A2 -i seeed
```

Noter le périphérique (`plughw:CARD=seeed2micvoicec,DEV=0` avec le pilote d'origine). Test :

```bash
arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -r 16000 -c 1 -f S16_LE -t wav -d 5 test.wav
aplay -D plughw:CARD=seeed2micvoicec,DEV=0 test.wav
```

Si l'enregistrement est faible, monter le gain de capture dans `alsamixer` (F6 → la carte
seeed, F4 → capture), puis `sudo alsactl store`.

### 3.2 Installer wyoming-satellite

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends git python3-venv
cd ~
git clone --branch v1.4.1 https://github.com/rhasspy/wyoming-satellite.git
cd wyoming-satellite
python3 -m venv .venv
.venv/bin/pip3 install --upgrade pip wheel setuptools
.venv/bin/pip3 install --extra-index-url https://www.piwheels.org/simple \
  -f 'https://synesthesiam.github.io/prebuilt-apps/' -e '.[webrtc]'
```

`[webrtc]` apporte le contrôle de gain et la réduction de bruit (`--mic-auto-gain`,
`--mic-noise-suppression`). Si son installation échoue (OS 32 bits), installer avec `-e .` et
retirer ces deux options des fichiers de service.

Test manuel (Ctrl+C pour arrêter) :

```bash
script/run --debug --name salon --uri 'tcp://0.0.0.0:10700' --no-zeroconf \
  --mic-command 'arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -r 16000 -c 1 -f S16_LE -t raw' \
  --snd-command 'aplay -D plughw:CARD=seeed2micvoicec,DEV=0 -r 22050 -c 1 -f S16_LE -t raw'
```

Il attend la connexion du pont : c'est normal qu'il ne se passe rien tant que le pont ne tourne pas.

### 3.3 Service systemd

```bash
sudo cp ~/jeedom-voice/pi/systemd/wyoming-satellite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wyoming-satellite
journalctl -u wyoming-satellite -f
```

### 3.4 Mode B : wake word sur le Pi

```bash
sudo apt-get install --no-install-recommends libopenblas-dev
cd ~
git clone --branch v2.1.0 https://github.com/rhasspy/wyoming-openwakeword.git
cd wyoming-openwakeword
script/setup
mkdir -p custom
sudo cp ~/jeedom-voice/pi/systemd/wyoming-openwakeword.service /etc/systemd/system/
```

Dans `/etc/systemd/system/wyoming-satellite.service` : commenter le `ExecStart` du mode A,
décommenter celui du mode B et les lignes `Requires=`/`After=wyoming-openwakeword.service`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wyoming-openwakeword
sudo systemctl restart wyoming-satellite
```

Wake words fournis : `okay_nabu`, `hey_jarvis`, `alexa`, `hey_mycroft`… (anglais). Pour un
wake word français, entraîner un modèle avec openWakeWord, déposer le `.tflite` dans `custom/`
et passer son nom de fichier (sans extension) à `--wake-word-name`.

### 3.5 LED du HAT (optionnel)

```bash
sudo raspi-config nonint do_spi 0      # Raspberry Pi OS ; sinon dtparam=spi=on dans /boot/firmware/config.txt + reboot
sudo apt-get install python3-spidev python3-gpiozero
cd ~/wyoming-satellite/examples
python3 -m venv --system-site-packages .venv
.venv/bin/pip3 install 'wyoming==1.5.2'
sudo cp ~/jeedom-voice/pi/systemd/2mic-leds.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now 2mic-leds
```

Puis ajouter `--event-uri 'tcp://127.0.0.1:10500'` au `ExecStart` du satellite et
décommenter `Requires=2mic-leds.service`. Le script d'exemple vise la carte d'origine (v1) :
si les LED restent éteintes, vérifier SPI et la révision de la carte.

### 3.6 Pare-feu (indispensable)

Wyoming n'authentifie personne : n'importe quelle machine du réseau qui se connecte au port
10700 peut écouter le micro. Seul le container doit y avoir accès.

```bash
sudo apt-get install nftables
sudo mkdir -p /etc/nftables.d
sudo cp ~/jeedom-voice/pi/nftables-satellite.nft /etc/nftables.d/satellite.nft
sudo nano /etc/nftables.d/satellite.nft          # remplacer 192.168.1.20 par l'IP du container
echo 'include "/etc/nftables.d/*.nft"' | sudo tee -a /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft list ruleset
```

Donner une IP fixe (bail DHCP réservé) au Pi et au container.

## 4. Container LXC

Commandes en root, Debian 12 (Python 3.11), archive décompressée dans `/root/jeedom-voice`.
Prévoir 4 vCPU pour Whisper `small`.

```bash
tar xzf jeedom-voice.tar.gz -C /root
apt-get update
apt-get install --no-install-recommends git python3-venv
useradd --system --home-dir /opt/voice-bridge --shell /usr/sbin/nologin voice
mkdir -p /var/lib/wyoming/whisper /var/lib/wyoming/piper
chown -R voice:voice /var/lib/wyoming
```

### 4.1 Whisper (déjà installé)

Le pont a besoin d'un `wyoming-faster-whisper` joignable (de préférence lié à `127.0.0.1`).
Si le vôtre est lancé à la main, `container/systemd/wyoming-faster-whisper.service` donne un
modèle de service : remplacer son `ExecStart` par votre commande actuelle, en gardant
`--uri 'tcp://127.0.0.1:10300'` et `--language fr`.

Conseils Whisper en français :
- `--model small` est le bon compromis sur CPU ; `tiny`/`base` comprennent mal le français ;
  `medium` est plus juste mais nettement plus lent ;
- `--initial-prompt` avec le vocabulaire de la maison (pièces, équipements) améliore la
  reconnaissance des noms propres ;
- `--vad-filter` limite les hallucinations sur le bruit.

### 4.2 openWakeWord (mode A seulement)

```bash
cd /opt
git clone --branch v2.1.0 https://github.com/rhasspy/wyoming-openwakeword.git
cd wyoming-openwakeword && script/setup && mkdir -p custom
cp /root/jeedom-voice/container/systemd/wyoming-openwakeword.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now wyoming-openwakeword
```

### 4.3 Piper (optionnel, réponse vocale)

```bash
cd /opt
git clone --branch v2.5.2 https://github.com/rhasspy/wyoming-piper.git
cd wyoming-piper && script/setup
cp /root/jeedom-voice/container/systemd/wyoming-piper.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now wyoming-piper
```

La voix est téléchargée au premier démarrage (`journalctl -u wyoming-piper`), puis tout est
local. Voix françaises : `fr_FR-siwis-medium`, `fr_FR-upmc-medium`, `fr_FR-tom-medium`,
`fr_FR-mls-medium`, `fr_FR-gilles-low`, `fr_FR-siwis-low`.

### 4.4 Le pont

```bash
mkdir -p /opt/voice-bridge /etc/voice-bridge
cp /root/jeedom-voice/container/voice_bridge.py /root/jeedom-voice/container/requirements.txt /opt/voice-bridge/
cp -r /root/jeedom-voice/container/tools /opt/voice-bridge/
python3 -m venv /opt/voice-bridge/.venv
/opt/voice-bridge/.venv/bin/pip install -r /opt/voice-bridge/requirements.txt
cp /root/jeedom-voice/container/config.example.toml /etc/voice-bridge/config.toml
chown root:voice /etc/voice-bridge/config.toml && chmod 640 /etc/voice-bridge/config.toml
nano /etc/voice-bridge/config.toml
```

Dans `config.toml` : URL de Jeedom, clé API, IP du satellite ; retirer `[wake]` en mode B et
`[tts]` sans Piper. Puis :

```bash
/opt/voice-bridge/.venv/bin/python /opt/voice-bridge/voice_bridge.py --config /etc/voice-bridge/config.toml --check
/opt/voice-bridge/.venv/bin/python /opt/voice-bridge/voice_bridge.py --config /etc/voice-bridge/config.toml --ask "quelle est la température du salon"
```

`--check` interroge Whisper, openWakeWord, Piper, chaque satellite et Jeedom (requête vide :
valide la clé API sans rien déclencher). `--ask` envoie une phrase à Jeedom et affiche sa réponse.

```bash
cp /root/jeedom-voice/container/systemd/voice-bridge.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now voice-bridge
journalctl -u voice-bridge -f
```

Journal attendu après « okay nabu, allume la lumière du salon » :

```
voice_bridge.salon: Connecté au satellite tcp://192.168.1.50:10700
voice_bridge.salon: Wake word détecté : okay_nabu
voice_bridge.salon: Commande : « allume la lumière du salon »
voice_bridge.salon: Réponse Jeedom : « C'est fait »
```

### 4.5 Tester le container sans le Pi

`tools/fake_satellite.py` se fait passer pour un satellite et rejoue un WAV (enregistré sur le
Pi avec `arecord … -t wav commande.wav`, par exemple) :

```bash
cd /opt/voice-bridge
.venv/bin/python tools/fake_satellite.py commande.wav --uri tcp://127.0.0.1:10799 --tts-output reponse.wav
```

Dans un second terminal, lancer le pont avec une copie de la configuration dont le satellite
pointe sur `tcp://127.0.0.1:10799`. Par défaut le faux satellite simule le mode B (le WAV ne
contient que la commande) ; `--remote-wake` simule le mode A (le WAV doit commencer par le
wake word).

## 5. Jeedom

1. **Réglages → Système → Configuration**, onglet API : copier la clé API de Jeedom (core) et
   vérifier que l'accès à l'API JSON-RPC n'est pas désactivé. Le restreindre de préférence aux
   IP blanches, avec l'IP du container.
2. **Outils → Interactions** : créer les phrases (`allume la lumière du #objet#`, synonymes…)
   et les tester avec le bouton de test puis `voice_bridge.py --ask`.
3. Option `reply_cmd` dans `[jeedom]` : id d'une commande Jeedom qui reçoit aussi la réponse
   (notification, TTS d'un autre équipement…).
4. Option `profile` par satellite : profil transmis au moteur d'interaction.

La clé API part dans le corps de la requête POST, pas dans l'URL, pour ne pas apparaître dans
les journaux du serveur web.

## 6. Réglages fins

- **Fin de phrase coupée trop tôt** : monter `silence_seconds` (0.8 → 1.2).
- **Commande jamais terminée dans le bruit** : monter `aggressiveness` (2 → 3).
- **Faux réveils** : `--threshold 0.6` ou `--trigger-level 2` sur openWakeWord.
- **Phrases fantômes de Whisper** (« Sous-titres réalisés par… ») : déjà filtrées, compléter
  `ignored_phrases` si d'autres apparaissent.
- **Analyse audio** : `--debug-recording-dir /tmp/rec` sur le satellite enregistre ce qu'il
  envoie ; `--debug` sur le pont détaille le pipeline.

## 7. Dépannage

| Symptôme (journal) | Cause probable |
|---|---|
| `Satellite … indisponible : Connect call failed` | satellite arrêté, IP fausse, ou pare-feu du Pi qui n'autorise pas l'IP du container |
| Satellite : `Did not receive ping response within timeout` | réseau instable ou container surchargé |
| Mode A, jamais de `Wake word détecté` | `[wake]` absent de la config, service openWakeWord arrêté, ou micro trop faible |
| `Aucune parole après le wake word` | gain micro trop bas, ou `speech_seconds` trop exigeant |
| `Jeedom n'a pas répondu : Vous n'êtes pas autorisé…` | clé API, accès JSON-RPC désactivé, ou IP du container hors IP blanches |
| Jeedom répond « Désolé je n'ai pas compris » | phrase absente des interactions : comparer la `Commande` du journal avec les interactions |
| Un second pont ne se connecte jamais | un satellite n'accepte qu'un contrôleur à la fois |

## Contenu

```
container/voice_bridge.py           pont Wyoming -> Jeedom
container/config.example.toml       configuration commentée
container/requirements.txt          wyoming 1.11.0, webrtcvad-wheels
container/tools/fake_satellite.py   faux satellite pour les tests
container/systemd/                  voice-bridge, faster-whisper, openwakeword, piper
pi/systemd/                         wyoming-satellite (modes A et B), openwakeword, LED
pi/nftables-satellite.nft           pare-feu du port 10700
```
