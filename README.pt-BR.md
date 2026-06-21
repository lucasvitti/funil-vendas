[English](README.md) | **Português**

# counter_vision

**Contagem de pessoas + detecção de permanência** multi-câmera para um ponto de
atendimento no varejo físico, em dois níveis:

- **Unidade de borda (edge)** — um gabinete móvel (Raspberry Pi 5 + AI HAT+ / Hailo
  + câmeras CSI grande-angulares) detecta, rastreia e conta o **fluxo** (pessoas
  cruzando uma linha tripwire) e as **paradas** (pessoas que permanecem ≥ N segundos
  numa zona), com rostos **pixelizados no dispositivo** antes de qualquer coisa ser
  salva (LGPD).
- **Hub central (VPS)** — um serviço FastAPI + SQLite recebe as contagens anônimas
  (e, no piloto, vetores de re-ID + snapshots pixelizados) e serve um **editor de
  geometria/configuração hospedado** e um **dashboard de conversão**. O board
  **puxa sua configuração do hub no boot**.

Uma pergunta por ponto de atendimento: **das pessoas que passam, quantas param — e
por quanto tempo?** (conversão = paradas ÷ transeuntes).

**Build da Fase 1: 2× câmeras CSI grande-angulares → ~180°** (unidade encostada em
parede/balcão). O Pi 5 tem apenas 2 portas CSI, então 2 é o máximo simultâneo
nativo. O código é **N-câmeras** por toda parte, então um deploy futuro de **360°**
(4 câmeras) é uma mudança de hardware + configuração, não uma reescrita. Veja
"Futuro 360°".

## Arquitetura

```
UNIDADE DE BORDA — Raspberry Pi 5 + AI HAT+ (Hailo) + 2 câmeras CSI wide (~180°)
   por câmera, de forma independente:
   captura ─► detecção de pessoa (Hailo) ─► tracking (ByteTrack)
        │   geometria de contagem (puxada do hub), por câmera:
        ▼   tripwire → fluxo        zona + permanência ≥ N s → parada
   rostos pixelizados no dispositivo (LGPD) → SQLite local + snapshots + vetores re-ID
        │
        │   count.py + upload_to_server.py  — systemd, auto-restart
        ▼   HTTPS + token Bearer, SOMENTE SAÍDA (board atrás de NAT)
HUB CENTRAL — VPS: FastAPI + SQLite atrás de nginx + Let's Encrypt
   ├─ /config   editor de geometria + parâmetros → board puxa no boot
   ├─ /         dashboard de conversão (intervalo · granularidade · local ·
   │            imagens/vetores · paginação · atividade · downloads · purga)
   └─ /api/*    eventos · vetores · snapshots · frames · config · capture
```

## Como a contagem funciona (setores por câmera, não fusão multi-visão)

As câmeras observam setores *diferentes* (2 cobrindo ~180° agora; até 4 para 360°
depois), então uma pessoa normalmente está no campo de uma câmera por vez — sem
necessidade de fundir visões simultâneas do mesmo ponto. Cada câmera roda seu
próprio pipeline e conta usando a geometria desenhada na sua própria imagem:

- **Fluxo (footfall)** — uma pessoa cruzando uma **linha de passagem (tripwire)**
  (entrando na área).
- **Paradas** — uma pessoa que permanece ≥ N segundos dentro de uma **zona** (um
  ponto de atendimento). Também registramos **quanto tempo** cada pessoa fica
  (duração da permanência), não apenas uma parada binária → média/mediana/maior/
  distribuição do tempo no balcão.

As contagens por câmera são agregadas. Isso deliberadamente abandona a fusão por
homografia / coordenadas de mundo anterior: ela exigia câmeras fixas e calibradas e
trazia robustez a oclusão que não precisamos para cobertura apontada para fora.
Trade-off: um leque para fora não enxerga *ao redor* das pessoas em um balcão único
e cheio (cada ponto é visto por ~1 câmera).

### Como a contagem dupla é evitada (dedup por geometria, não por re-ID)

As câmeras se sobrepõem em *campo de visão* (necessário para ladrilhar 360°/180°
sem falhas), então uma pessoa numa sobreposição é *vista* por 2 câmeras. A solução
é contar **cruzamentos de linha**, não presença, e tornar a geometria de contagem
**sem sobreposição por construção**:

- A fronteira de contagem é **um anel** (360°) ou **uma linha** (180°) dividida em
  **arcos/segmentos disjuntos, um por câmera**. Cruzá-la dispara exatamente uma
  contagem, de posse do único arco cruzado — independentemente de quantas câmeras
  veem a pessoa.
- **Cruzamento direcional (entrada/saída) + cooldown por ID de track** para que
  tremores na linha, ou passar direto (1 entrada + 1 saída), resultem corretamente.
- Cada **zona de permanência pertence a exatamente uma câmera**. Um ponto de
  atendimento numa sobreposição vai para a câmera com melhor visão; a outra o ignora.
- Apenas casos genuinamente sobre a emenda precisam de um **hand-off de fronteira**
  aproximado (associar um track saindo da emenda da câmera A a um entrando na câmera
  B dentro de uma curta janela de tempo, via assinatura barata de cor/aparência) —
  opcional. Sem re-ID completo entre câmeras.

O editor de geometria hospedado impõe a única invariante: **linhas/zonas não podem
se sobrepor em cobertura.** Essa única regra é o que garante a ausência de contagem
dupla.

## Privacidade & LGPD

Dados pessoais (rostos, vetores de re-ID, imagens) são minimizados e estritamente
delimitados:

- **Rostos pixelizados no dispositivo** antes de qualquer quadro ser salvo ou
  enviado — nenhum rosto bruto sai do board.
- **As contagens são agregados anônimos** (eventos de passagem/parada; sem identidade).
- **O re-ID (piloto) é uma assinatura de cor de roupa/corpo, não biométrico** —
  opt-in, comparado apenas dentro de uma janela curta, auto-apagado após um período
  de retenção; os modos face/full (biométricos) são restritos.
- **Purga de dados**: o dashboard apaga vetores + imagens pixelizadas de um
  intervalo de datas escolhido; o servidor também auto-deleta ambos após
  `COUNTER_RETAIN_DAYS`.
- **Rótulo de "local" por dispositivo** é registrado em cada evento/vetor/imagem,
  preservando o histórico de localização quando a unidade móvel é realocada.
- Token nunca no repositório (env / `~/.counter_token`); TLS + HSTS em tudo;
  backend restrito a localhost atrás do nginx. Um aviso afixado no balcão + uma
  política de retenção documentada fazem parte do deploy.

## A unidade de borda (agnóstica de placa por design)

O código abstrai as duas camadas específicas de hardware para que a escolha de
detector/câmera possa mudar sem tocar no pipeline:

- **Fonte de câmera** (`src/cameras/`): `USBCamera` (OpenCV) para dev no laptop, e
  `Picamera2Camera` (CSI, no Pi). Ambas são subclasses de `CameraSource`; o pipeline
  nunca vê a diferença.
- **Detector** (`src/detect/`): backend plugável — `cpu` (Ultralytics, para dev no
  laptop) e `hailo` (HailoRT no AI HAT+, produção no Pi).

**Hardware: uma unidade móvel conectada = Raspberry Pi 5 + AI HAT+ 13 TOPS
(Hailo-8L) + 2× câmeras CSI grande-angulares**, num case impresso em 3D. O Hailo
roda detecção de pessoas YOLOv8 nos dois streams em tempo real com folga enorme —
suficiente para um **modelo maior/mais preciso** (YOLOv8s/m), já que apenas 2
streams compartilham 13 TOPS. O HAT fica em **PCIe**; as câmeras em **CSI** — então
todas as portas USB ficam livres e nenhum hub é necessário.

**Câmeras: 2× módulos CSI grande-angulares.** Recomendado: **Raspberry Pi Camera
Module 3 Wide** (IMX708, ~120° diagonal ≈ ~102° horizontal, autofoco). Duas a ~90°
de distância cobrem ~180°. ⚠️ Pegue a variante **Wide** — o Module 3 padrão tem só
~66° horizontal e deixaria uma falha. Módulos Arducam wide IMX219 também funcionam.

⚠️ **Cabo de câmera do Pi 5:** o Pi 5 usa o conector CSI **estreito de 22 pinos**,
mas as câmeras vêm com flat de **15 pinos** — compre um **cabo adaptador 15→22
pinos para Pi 5** por câmera.

Backend de detector = **HailoRT no AI HAT+**: suporte oficial no Pi OS via o pacote
apt **`hailo-all`** (HailoRT + driver PCIe + ferramentas); use um **`.hef`
YOLOv8/person pré-compilado** do model zoo da Hailo (o Dataflow Compiler é só x86
caso você venha a construir um `.hef` customizado).

### Futuro 360°

As 2 portas CSI limitam o Pi 5 a 2 câmeras simultâneas. Para chegar a 360° (4
câmeras) depois — **sem reescrever código** (o pipeline já é N-câmeras) — troque
para um **Compute Module 5 + carrier de 4 CSI**, ou **importe módulos de câmera
USB** (Arducam B0201 / ELP, UVC+MJPEG, ~90–120°, evite olho-de-peixe) e adicione um
hub com alimentação. Projete o case 3D com um **suporte frontal de câmera trocável**
para que um anel de 4 câmeras possa substituir a frente de 2 câmeras depois.

## O hub central (VPS)

Um pequeno serviço FastAPI + SQLite (`server/`) é o hub. Os boards o alcançam
**apenas de saída (outbound)** por HTTPS com um token Bearer — o Pi não precisa de
portas de entrada nem IP público. Ele oferece:

- **Editor de geometria + configuração hospedado** (`/config`) — por câmera:
  desenhe a **linha tripwire** + a **zona de permanência** sobre um quadro de
  referência ao vivo (ou uma **imagem enviada**), **gire** para deixar na vertical
  (a geometria gira junto), **recolha/expanda** os blocos, e ajuste parâmetros
  (permanência, confiança, re-ID, uploads, **nome do local/loja**, tempos do board)
  agrupados por categoria com dicas inline. **"Take shot"** puxa um quadro fresco do
  board sob demanda. A config salva — geometria, rotação e parâmetros — é **puxada
  pelo board no próximo boot**.
- **Dashboard de conversão** (`/`) — transeuntes / paradas / conversão agregados a
  partir dos eventos brutos, com filtro de **intervalo de datas**, **granularidade
  de 10/30/60 min**, filtro de **local**, colunas por bucket (data, hora, local,
  transeuntes, paradas, conversão, imagens, vetores), **paginação**, indicador de
  **atividade do board** (último contato), downloads **CSV / ZIP de imagens**, e
  **purga de dados por intervalo de datas**.
- **Autenticação** — cookie de sessão (login) para a UI; token Bearer para os
  endpoints `/api/*` do board.

**Deploy:** `server/` traz um `Dockerfile` + `docker-compose.yml` (env
`COUNTER_TOKEN`, `COUNTER_USER`, `COUNTER_PASS`). Escuta em localhost; um vhost do
nginx (`server/nginx-topofunil.conf`) termina TLS (Let's Encrypt) + HSTS e faz
reverse-proxy. **Rebuild ao mudar com `docker compose up -d --build`** — um
`restart` simples mantém a imagem antiga.

## Rodando

### No board — produção (systemd)

Dois **serviços de usuário do systemd** (`systemd/`) iniciam no boot e reiniciam em
falha:
```bash
cp systemd/*.service ~/.config/systemd/user/
loginctl enable-linger                 # iniciar no boot sem login interativo
systemctl --user daemon-reload
systemctl --user enable --now counter counter-upload
journalctl --user -u counter -f        # logs
```
`counter.service` → `count.py` (câmeras + Hailo + contagem + frames sob demanda);
`counter-upload.service` → `upload_to_server.py --loop` (eventos/vetores/imagens/frames → hub).

### No board — manual
```bash
sudo apt install -y python3-picamera2 hailo-all
python3 -m venv --system-site-packages .venv   # para enxergar picamera2 + hailo
. .venv/bin/activate
pip install supervision PyYAML opencv-python    # NÃO ultralytics — o Hailo faz a detecção
python count.py config.pi.yaml                  # ao vivo: transeuntes / paradas / conversão + log SQLite
python report.py                                # resumo de estatísticas de permanência
```

### Dev no laptop (sem Pi)
```bash
python -m pip install -r requirements.txt       # inclui ultralytics + supervision
python detect_preview.py                        # config.yaml: cam0 = webcam, backend: cpu
```

### O hub (VPS)
```bash
cd server
cp .env.example .env      # COUNTER_TOKEN (openssl rand -hex 32) + COUNTER_USER / COUNTER_PASS
docker compose up -d --build
```

## Status do build

- [x] **Captura + detecção + tracking na borda** — N-câmeras; Hailo (Pi) / CPU (dev).
- [x] **Contagem** — fluxo por tripwire + permanência em zona com duração por
      pessoa; conversão; eventos → `data/counts.sqlite`.
- [x] **Editor de geometria + configuração hospedado** — desenhar / girar /
      enviar-imagem / take-shot; parâmetros; puxado pelo board no boot.
- [x] **Hub central + dashboard** — conversão por data/hora/local, imagens/vetores,
      paginação, atividade, downloads, purga.
- [x] **Privacidade / LGPD** — pixelização de rosto no dispositivo, contagens
      anônimas, retenção, purga por intervalo.
- [x] **Transformado em serviço** — serviços de usuário do systemd (auto-start +
      restart em falha).
- [~] **Re-ID (piloto)** — vetores de roupa/corpo + janela de comparação efêmera,
      em validação; modos biométricos restritos.
- [ ] **Hand-off de fronteira** para tracks sobre a emenda (opcional) + **360°**
      (4 câmeras via CM5 / USB).
