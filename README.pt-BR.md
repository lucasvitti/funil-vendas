[English](README.md) | **Português**

# counter_vision

**Contagem de pessoas + detecção de permanência** multi-câmera a partir de uma única unidade móvel. Um gabinete abriga um Raspberry Pi 5 + AI HAT+ (Hailo) + câmeras grande-angulares apontadas **para fora**, cobrindo uma área. Objetivo: contar o **fluxo de pessoas** (transeuntes na área/ao redor) *e* as **paradas** (pessoas que permanecem ≥ N segundos em um ponto de atendimento).

**Build da Fase 1: 2× câmeras CSI grande-angulares → ~180°** (unidade encostada em parede/balcão). O Pi 5 tem apenas 2 portas CSI, então 2 é o máximo simultâneo nativo. O código é **N-câmeras** por toda parte, então um deploy futuro de **360°** (4 câmeras) é uma mudança de hardware + configuração, não uma reescrita — via Compute Module 5 + carrier de 4 CSI, ou módulos de câmera USB importados. Veja "Futuro 360°" abaixo.

## Como a contagem funciona (setores por câmera, não fusão multi-visão)

As câmeras observam setores *diferentes* (2 cobrindo ~180° agora; até 4 para 360° depois), então uma pessoa normalmente está no campo de uma câmera por vez — sem necessidade de fundir visões simultâneas do mesmo ponto. Cada câmera roda seu próprio pipeline e conta usando a geometria desenhada na sua própria imagem:

- **Fluxo (footfall)** — uma pessoa cruzando uma **linha de passagem (tripwire)** (entrando na área).
- **Paradas** — uma pessoa que permanece ≥ N segundos dentro de uma **zona** (um ponto de atendimento). Também registramos **quanto tempo** cada pessoa fica (duração da permanência), não apenas uma parada binária → média/mediana/maior/distribuição do tempo no balcão.

As contagens por câmera são agregadas. Isso deliberadamente abandona a fusão por homografia / coordenadas de mundo anterior: ela exigia câmeras fixas e calibradas e trazia robustez a oclusão que não precisamos para cobertura apontada para fora. Trade-off: um leque para fora não enxerga *ao redor* das pessoas em um balcão único e cheio (cada ponto é visto por ~1 câmera).

### Como a contagem dupla é evitada (dedup por geometria, não por re-ID)

As câmeras se sobrepõem em *campo de visão* (necessário para ladrilhar 360°/180° sem falhas), então uma pessoa numa sobreposição é *vista* por 2 câmeras. A solução é contar **cruzamentos de linha**, não presença, e tornar a geometria de contagem **sem sobreposição por construção**:

- A fronteira de contagem é **um anel** (360°) ou **uma linha** (180°) dividida em **arcos/segmentos disjuntos, um por câmera**. Cruzá-la dispara exatamente uma contagem, de posse do único arco cruzado — independentemente de quantas câmeras veem a pessoa.
- **Cruzamento direcional (entrada/saída) + cooldown por ID de track** para que tremores na linha, ou passar direto (1 entrada + 1 saída), resultem corretamente.
- Cada **zona de permanência pertence a exatamente uma câmera**. Um ponto de atendimento numa sobreposição vai para a câmera com melhor visão; a outra o ignora.
- Apenas casos genuinamente sobre a emenda precisam de um **hand-off de fronteira** aproximado (associar um track saindo da emenda da câmera A a um entrando na câmera B dentro de uma curta janela de tempo, via uma assinatura barata de cor/aparência) — opcional, Fase 4. Sem re-ID completo entre câmeras.

O editor de geometria da Fase 3 impõe a única invariante: **linhas/zonas não podem se sobrepor em cobertura.** Essa única regra é o que garante a ausência de contagem dupla.

```
N câmeras (para fora: 2→180° agora, até 4→360° depois)
        │   por câmera, de forma independente:
        ▼
   detecção de pessoa (Hailo / CPU dev) ─► tracking (ByteTrack)
        │
        ▼   geometria de contagem desenhada na imagem de cada câmera:
   linha tripwire → fluxo               zona + permanência ≥ N s → parada
        │
        ▼
   agrega entre câmeras + dedup de fronteira nas sobreposições de setor
        │
        ▼
   contagens (fluxo / paradas por intervalo) + snapshots de amostra
        │
        ▼
   SQLite/CSV local ──(rsync opc.)──► dashboard na VPS
```

## Agnóstico de placa por design

O código abstrai as duas camadas específicas de hardware para que a escolha de detector/câmera possa mudar sem tocar no pipeline:

- **Fonte de câmera** (`src/cameras/`): `USBCamera` (OpenCV) para dev no laptop, e `Picamera2Camera` (CSI, no Pi). Ambas são subclasses de `CameraSource`; o pipeline nunca vê a diferença.
- **Detector** (`src/detect/`, Fase 2): backend plugável — `cpu` (Ultralytics, para dev no laptop) e `hailo` (HailoRT no AI HAT+, produção no Pi).

**Decisão de hardware: uma unidade móvel conectada = Raspberry Pi 5 + AI HAT+ 13 TOPS (Hailo-8L) + 2× câmeras CSI grande-angulares**, num case impresso em 3D. Pi 5 do Mercado Livre; AI HAT+ da MakerHero. O Hailo roda detecção de pessoas YOLOv8 nos dois streams em tempo real com folga enorme — suficiente para rodar um **modelo maior/mais preciso** (YOLOv8s/m), já que apenas 2 streams compartilham 13 TOPS. 4 GB de RAM são suficientes (inferência no Hailo). O HAT fica em **PCIe**; as câmeras em **CSI** — então todas as portas USB ficam livres e nenhum hub é necessário.

**Câmeras: 2× módulos CSI grande-angulares.** Recomendado: **Raspberry Pi Camera Module 3 Wide** (IMX708, ~120° diagonal ≈ ~102° horizontal, autofoco). Duas a ~90° de distância cobrem ~180°. ⚠️ Pegue a variante **Wide** — o Module 3 padrão tem só ~66° horizontal e deixaria uma falha. Módulos Arducam wide IMX219 também funcionam.

⚠️ **Cabo de câmera do Pi 5:** o Pi 5 usa o conector CSI **estreito de 22 pinos**, mas as câmeras vêm com flat de **15 pinos** — compre um **cabo adaptador 15→22 pinos para Pi 5** por câmera.

Backend de detector = **HailoRT no AI HAT+**:
- Suporte oficial no Pi OS via o pacote apt **`hailo-all`** (HailoRT + driver PCIe + ferramentas) — uma stack mantida e tranquila.
- Use um **`.hef` YOLOv8/person pré-compilado** do model zoo da Hailo (sem auto-compilação no caso comum). O Hailo Dataflow Compiler é só x86 caso você venha a construir um `.hef` customizado.
- O HAT empilha acima do Pi 5 (FPC PCIe + standoffs GPIO, acima do cooler ativo) → o case 3D precisa permitir a altura extra da pilha.

### Futuro 360°

As 2 portas CSI limitam o Pi 5 a 2 câmeras simultâneas. Para chegar a 360° (4 câmeras) depois — **sem reescrever código** (o pipeline já é N-câmeras) — troque para um **Compute Module 5 + carrier de 4 CSI**, ou **importe módulos de câmera USB** (Arducam B0201 / ELP, UVC+MJPEG, ~90–120°, evite olho-de-peixe) e adicione um hub com alimentação. Projete o case 3D com um **suporte frontal de câmera trocável** para que um anel de 4 câmeras possa substituir a frente de 2 câmeras depois.

Dev/teste roda numa webcam de laptop Windows/Linux comum (detector CPU) antes de as peças chegarem; o backend Hailo entra no lugar no Pi via a camada plugável de detector.

## Fases do build

- [x] **Fase 1 — Scaffold e captura multi-câmera.** Abre N câmeras USB, captura conjuntos de quadros quase sincronizados, salva em disco. Prova que câmeras + config funcionam.
- [x] **Fase 2 — Detecção + tracking por câmera.** Detecção de pessoas YOLOv8 (`cpu` Ultralytics para dev / `hailo` no AI HAT+) + ByteTrack do supervision, com previews anotados. Veja "Rodando a Fase 2".
- [ ] **Fase 3 — Editor de geometria de contagem** (desenhar uma linha tripwire + zona de permanência por câmera, no espaço da imagem; salvo na config — rápido de refazer ao reposicionar).
- [x] **Fase 4 — Lógica de contagem.** Cruzamentos de tripwire = transeuntes (fluxo), permanência na zona ≥ N s = paradas com **duração** por pessoa; conversão = paradas ÷ transeuntes. Eventos → `data/counts.sqlite`; `report.py` resume estatísticas de permanência. Veja "Rodando a Fase 4".
- [ ] **Fase 5 — Saída + LGPD** (contagens para SQLite/CSV, snapshots com limite de taxa e auto-deleção / desfoque de rosto).
- [ ] **Fase 6 — Transformar em serviço** (unit do systemd, config recarregável em runtime).

## Rodando a Fase 1

```bash
# 1. Instale as dependências (recomenda-se um venv)
python -m pip install -r requirements.txt

# 2. Edite config.yaml — aponte uma câmera para a webcam do seu laptop (source: 0)
#    No Windows, backend: dshow costuma funcionar melhor.

# 3. Capture
python capture.py
# Salva um conjunto de quadros com timestamp a cada interval_seconds em data/snapshots/.
# Ctrl+C para parar.
```

## Rodando a Fase 2 (detecção + tracking)

Por câmera: captura → detecção → ByteTrack → `data/annotated/<cam_id>.jpg` anotado
(sobrescrito a cada ciclo) + contagens/FPS por câmera impressos no console.

**Dev no laptop (webcam USB + CPU YOLOv8):**
```bash
python -m pip install -r requirements.txt   # inclui ultralytics + supervision
python detect_preview.py                    # usa config.yaml (cam0 = webcam, backend: cpu)
```

**No Pi (2× câmeras CSI + Hailo):**
```bash
sudo apt install -y python3-picamera2 hailo-all
ls /usr/share/hailo-models/                 # confirme o caminho do yolov8s_h8l.hef (edite config.pi.yaml se diferente)

python3 -m venv --system-site-packages .venv   # para enxergar picamera2 + hailo
. .venv/bin/activate
pip install supervision PyYAML opencv-python    # NÃO ultralytics — o Hailo faz a detecção

python detect_preview.py config.pi.yaml
```

Veja os quadros anotados de forma headless puxando-os (`scp pi-cam.local:.../data/annotated/cam0.jpg .`)
ou servindo a pasta (`python -m http.server` em `data/annotated`).

> Pontos conhecidos de ajuste na primeira execução: o caminho exato do `.hef`, e a
> ordem de canais BGR/RGB em `src/detect/hailo.py` (alterne o `cvtColor` se as
> detecções parecerem fracas).

## Rodando a Fase 4 (contagem)

Precisa de `geometry.yaml` (desenhe com `tools/geometry_editor.html`; um placeholder
acompanha para testes de encanamento).

```bash
python count.py config.pi.yaml     # ao vivo: transeuntes / paradas / conversão + log SQLite
python report.py                   # resumo: transeuntes, paradas, conversão, permanência média/mediana/p90/máx
```
O preview anotado (tripwire + zona + HUD) cai em `data/annotated/<cam>.jpg`;
cada evento PASS/STOP é registrado em `data/counts.sqlite`.

## Nota sobre LGPD

Snapshots contêm imagens de pessoas identificáveis → dados pessoais sob a LGPD.
A Fase 5 incorpora: snapshots opt-in e com limite de taxa, auto-deleção após N dias,
desfoque de rosto opcional, e contagens armazenadas como agregados anônimos. Um aviso
afixado no balcão + uma política de retenção documentada fazem parte do deploy.
