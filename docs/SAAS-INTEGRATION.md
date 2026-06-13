# Integração: API de Transcrição no SaaS ViaProve

> **Instrução para a IA que vai implementar:** leia este documento por completo e integre a API de transcrição no **backend** do SaaS. Não exponha credenciais no frontend. Siga o fluxo assíncrono com polling descrito abaixo.

---

## 1. Contexto

O SaaS ViaProve precisa transcrever áudios em texto usando uma API externa já deployada.

**Casos de uso:**

| Caso | Origem do áudio | `source` | O que fazer com o texto |
|------|-----------------|----------|-------------------------|
| WhatsApp | Mensagem de áudio recebida no webhook WPP | `whatsapp` | Enviar para o chatbot/IA e responder ao usuário |
| Gravação de atendimento | Usuário grava na aba de atendimento | `recording` | Enviar para IA de sumarização e exibir resumo |

A API é **assíncrona**: envia o áudio → recebe `job_id` → consulta status em loop → obtém `text` quando `completed`.

---

## 2. Configuração de produção

Adicionar no `.env` do **backend** do SaaS (nunca no frontend):

```env
TRANSCRIBE_API_URL=https://transcribe.viaprove.com.br
TRANSCRIBE_API_KEY=<pegar-da-vps-env-API_KEY>
```

| Variável | Valor |
|----------|-------|
| Base URL | `https://transcribe.viaprove.com.br` |
| Autenticação | Header `Authorization: Bearer <TRANSCRIBE_API_KEY>` |
| Health check | `GET https://transcribe.viaprove.com.br/health` (sem auth) |

**Regras de segurança obrigatórias:**

- `TRANSCRIBE_API_KEY` **somente** em variáveis de ambiente do servidor
- **Nunca** retornar a API key para o browser
- **Nunca** chamar a API de transcrição direto do frontend
- Todo áudio passa pelo backend do SaaS, que repassa para a API

---

## 3. Endpoints da API de transcrição

### `POST /v1/transcribe`

Inicia transcrição. Retorna imediatamente com `job_id`.

- **Content-Type:** `multipart/form-data`
- **Auth:** obrigatório

| Campo | Tipo | Obrigatório | Descrição |
|-------|------|-------------|-----------|
| `file` | arquivo | Sim | Áudio: ogg, mp3, m4a, wav, webm, opus, aac, flac |
| `user_id` | string | Sim | ID do tenant/usuário no SaaS (para métricas) |
| `source` | string | Não | `whatsapp`, `recording` ou `other` |

**Resposta `202`:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued",
  "poll_url": "/v1/jobs/550e8400-e29b-41d4-a716-446655440000"
}
```

**Limites:**

| Limite | Valor |
|--------|-------|
| Tamanho máximo do arquivo | 50 MB |
| Duração máxima do áudio | 60 minutos |
| Rate limit | 10 jobs/minuto por `user_id` |

---

### `GET /v1/jobs/{job_id}`

Consulta status e resultado.

**Resposta quando `completed`:**

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "user_id": "user_123",
  "source": "whatsapp",
  "text": "Olá, gostaria de agendar uma consulta para amanhã...",
  "duration_seconds": 45.2,
  "processing_time_seconds": 18.7,
  "model": "small",
  "error_message": null,
  "created_at": "2026-06-13T15:00:00+00:00"
}
```

**Status possíveis:**

| Status | Ação no SaaS |
|--------|--------------|
| `queued` | Continuar polling |
| `processing` | Continuar polling; opcional: mostrar "Transcrevendo..." na UI |
| `completed` | Usar `text`; seguir fluxo (IA, sumarização, etc.) |
| `failed` | Logar `error_message`; informar usuário |

---

## 4. Fluxo de integração (obrigatório)

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐
│   Frontend  │────▶│ Backend SaaS │────▶│ transcribe.viaprove │
│  (gravação) │     │              │     │      .com.br        │
└─────────────┘     └──────────────┘     └─────────────────────┘
                           │
                           │ 1. POST /v1/transcribe (audio + user_id)
                           │ 2. Recebe job_id
                           │ 3. Loop GET /v1/jobs/{id} a cada 3s
                           │ 4. Quando completed → text
                           ▼
                    ┌──────────────┐
                    │   IA / LLM   │  sumarização ou chatbot
                    └──────────────┘
```

**Parâmetros de polling recomendados:**

| Parâmetro | Valor |
|-----------|-------|
| Intervalo entre consultas | 3 segundos |
| Timeout máximo (áudio curto WPP) | 5 minutos |
| Timeout máximo (gravação longa) | 45 minutos |

---

## 5. O que implementar no SaaS

### 5.1 Módulo cliente da API (criar)

Criar um serviço isolado, por exemplo:

```
src/services/transcribe-api.ts     (ou .js / .py conforme stack)
```

**Responsabilidades:**

1. `createTranscriptionJob(audioBuffer, filename, userId, source)` → retorna `job_id`
2. `getJobStatus(jobId)` → retorna objeto do job
3. `transcribeAndWait(audioBuffer, filename, userId, source, options?)` → retorna `text` (faz polling internamente)

### 5.2 Implementação de referência (TypeScript / Node.js)

```typescript
// src/services/transcribe-api.ts

const API_URL = process.env.TRANSCRIBE_API_URL!;
const API_KEY = process.env.TRANSCRIBE_API_KEY!;

type AudioSource = "whatsapp" | "recording" | "other";
type JobStatus = "queued" | "processing" | "completed" | "failed";

interface TranscribeJob {
  job_id: string;
  status: JobStatus;
  text?: string | null;
  duration_seconds?: number | null;
  processing_time_seconds?: number | null;
  error_message?: string | null;
}

interface TranscribeOptions {
  pollIntervalMs?: number;
  timeoutMs?: number;
}

export async function createTranscriptionJob(
  audioBuffer: Buffer,
  filename: string,
  userId: string,
  source: AudioSource = "other"
): Promise<string> {
  const form = new FormData();
  form.append("file", new Blob([audioBuffer]), filename);
  form.append("user_id", userId);
  form.append("source", source);

  const res = await fetch(`${API_URL}/v1/transcribe`, {
    method: "POST",
    headers: { Authorization: `Bearer ${API_KEY}` },
    body: form,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Transcribe API error: ${res.status}`);
  }

  const data = await res.json();
  return data.job_id;
}

export async function getTranscriptionJob(jobId: string): Promise<TranscribeJob> {
  const res = await fetch(`${API_URL}/v1/jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${API_KEY}` },
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Job status error: ${res.status}`);
  }

  return res.json();
}

export async function transcribeAndWait(
  audioBuffer: Buffer,
  filename: string,
  userId: string,
  source: AudioSource = "other",
  options: TranscribeOptions = {}
): Promise<string> {
  const pollIntervalMs = options.pollIntervalMs ?? 3000;
  const timeoutMs = options.timeoutMs ?? (source === "recording" ? 45 * 60 * 1000 : 5 * 60 * 1000);

  const jobId = await createTranscriptionJob(audioBuffer, filename, userId, source);
  const deadline = Date.now() + timeoutMs;

  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollIntervalMs));

    const job = await getTranscriptionJob(jobId);

    if (job.status === "completed") {
      if (!job.text?.trim()) {
        throw new Error("Transcription completed but text is empty");
      }
      return job.text;
    }

    if (job.status === "failed") {
      throw new Error(job.error_message || "Transcription failed");
    }
  }

  throw new Error(`Transcription timed out after ${timeoutMs / 1000}s (job_id: ${jobId})`);
}
```

### 5.3 Implementação de referência (Python / FastAPI ou Flask)

```python
# services/transcribe_api.py

import os
import time
import httpx

API_URL = os.environ["TRANSCRIBE_API_URL"]
API_KEY = os.environ["TRANSCRIBE_API_KEY"]


def create_transcription_job(
    file_bytes: bytes,
    filename: str,
    user_id: str,
    source: str = "other",
) -> str:
    headers = {"Authorization": f"Bearer {API_KEY}"}

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{API_URL}/v1/transcribe",
            headers=headers,
            data={"user_id": user_id, "source": source},
            files={"file": (filename, file_bytes)},
        )
        response.raise_for_status()
        return response.json()["job_id"]


def get_transcription_job(job_id: str) -> dict:
    headers = {"Authorization": f"Bearer {API_KEY}"}

    with httpx.Client(timeout=30.0) as client:
        response = client.get(f"{API_URL}/v1/jobs/{job_id}", headers=headers)
        response.raise_for_status()
        return response.json()


def transcribe_and_wait(
    file_bytes: bytes,
    filename: str,
    user_id: str,
    source: str = "other",
    poll_interval: float = 3.0,
    timeout: float = 300.0,
) -> str:
    if source == "recording":
        timeout = 45 * 60.0

    job_id = create_transcription_job(file_bytes, filename, user_id, source)
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        job = get_transcription_job(job_id)

        if job["status"] == "completed":
            text = (job.get("text") or "").strip()
            if not text:
                raise RuntimeError("Transcription completed but text is empty")
            return text

        if job["status"] == "failed":
            raise RuntimeError(job.get("error_message") or "Transcription failed")

    raise TimeoutError(f"Transcription timed out (job_id: {job_id})")
```

---

## 6. Pontos de integração no SaaS

### 6.1 WhatsApp — áudio recebido

**Onde integrar:** handler do webhook que processa mensagens de áudio do WhatsApp.

**Fluxo:**

```
Webhook WPP recebe áudio
  → Backend baixa/decodifica o áudio (buffer + extensão, ex: .ogg)
  → transcribeAndWait(buffer, "audio.ogg", tenantId, "whatsapp")
  → text vai para o chatbot/IA
  → Resposta enviada ao usuário no WPP
```

**Exemplo:**

```typescript
// Dentro do handler de mensagem de áudio do WPP
const audioBuffer = await downloadWhatsAppAudio(message.audioId);
const text = await transcribeAndWait(
  audioBuffer,
  "audio.ogg",
  tenant.id,
  "whatsapp"
);
const aiResponse = await chatbot.processMessage(tenant.id, text);
await whatsapp.sendMessage(message.from, aiResponse);
```

**Importante:** processar de forma assíncrona (fila/background job) para não travar o webhook do WPP.

---

### 6.2 Gravação de atendimento — aba de atendimento

**Onde integrar:** endpoint do backend que recebe o upload/gravação de áudio do atendimento.

**Fluxo:**

```
Frontend grava áudio → POST /api/attendances/{id}/transcribe (backend SaaS)
  → Backend salva buffer temporário
  → transcribeAndWait(buffer, "recording.webm", userId, "recording", { timeoutMs: 45*60*1000 })
  → text vai para IA de sumarização
  → Backend salva resumo no banco
  → Frontend exibe resumo (polling ou websocket do próprio SaaS)
```

**Exemplo de endpoint no SaaS:**

```typescript
// POST /api/attendances/:id/transcribe
app.post("/api/attendances/:id/transcribe", async (req, res) => {
  const userId = req.user.id;
  const audioFile = req.file; // multer ou equivalente

  // Opção A: síncrono (simples, mas request fica aberto minutos)
  const text = await transcribeAndWait(
    audioFile.buffer,
    audioFile.originalname,
    userId,
    "recording",
    { timeoutMs: 45 * 60 * 1000 }
  );
  const summary = await aiService.summarize(text);
  await db.attendances.update(req.params.id, { transcript: text, summary });
  return res.json({ summary, transcript: text });

  // Opção B (recomendada para UX): retornar job_id do SaaS e processar em background
});
```

**Recomendação UX:** para gravações longas, usar **processamento em background** no SaaS:
1. Endpoint retorna imediatamente `{ status: "processing" }`
2. Worker interno faz polling na API de transcrição
3. Frontend consulta status do atendimento até sumarização ficar pronta

---

## 7. Tratamento de erros

| HTTP | Causa | Ação no SaaS |
|------|-------|--------------|
| `400` | Arquivo inválido, vazio, muito grande ou muito longo | Retornar erro amigável ao usuário |
| `401` | API key inválida | Logar erro crítico; alertar admin (config errada) |
| `404` | job_id não encontrado | Logar; não deveria acontecer em fluxo normal |
| `429` | Rate limit (10 jobs/min por user_id) | Retry com backoff ou enfileirar |
| `500` | Erro interno da API | Retry 1-2x; se persistir, logar e informar usuário |
| Timeout polling | Áudio muito longo ou fila cheia | Informar "transcrição em andamento, tente novamente" |

**Retry recomendado para erros 500/502/503:**

- Máximo 2 retries
- Backoff: 5s, 15s
- Não fazer retry em 400/401/404

---

## 8. Variáveis de ambiente no SaaS

```env
# Obrigatórias
TRANSCRIBE_API_URL=https://transcribe.viaprove.com.br
TRANSCRIBE_API_KEY=<sua-api-key-da-vps>

# Opcionais (com defaults no código)
TRANSCRIBE_POLL_INTERVAL_MS=3000
TRANSCRIBE_TIMEOUT_WPP_MS=300000
TRANSCRIBE_TIMEOUT_RECORDING_MS=2700000
```

---

## 9. Checklist para a IA implementar

- [ ] Criar módulo `transcribe-api` no backend com as 3 funções principais
- [ ] Adicionar `TRANSCRIBE_API_URL` e `TRANSCRIBE_API_KEY` no `.env.example` do SaaS
- [ ] Integrar no handler de **áudio do WhatsApp** com `source=whatsapp`
- [ ] Integrar no fluxo de **gravação de atendimento** com `source=recording`
- [ ] Passar `user_id` real do tenant/usuário em todas as chamadas
- [ ] Nunca expor API key no frontend
- [ ] Tratar erros 400, 401, 429, 500 e timeout de polling
- [ ] Para WPP: processar em background (não bloquear webhook)
- [ ] Para gravação longa: considerar background job + status na UI
- [ ] Após obter `text`, encaminhar para o serviço de IA já existente no SaaS
- [ ] Logar `job_id`, `duration_seconds` e `processing_time_seconds` para debug

---

## 10. Como testar após implementar

### Teste 1 — Health check (sem auth)

```bash
curl https://transcribe.viaprove.com.br/health
```

Esperado: `{"status":"ok","model":"small","queue_pending":0}`

### Teste 2 — Transcrição manual

```bash
curl -X POST "https://transcribe.viaprove.com.br/v1/transcribe" \
  -H "Authorization: Bearer $TRANSCRIBE_API_KEY" \
  -F "file=@audio.ogg" \
  -F "user_id=teste_1" \
  -F "source=whatsapp"
```

### Teste 3 — Consultar job

```bash
curl "https://transcribe.viaprove.com.br/v1/jobs/JOB_ID" \
  -H "Authorization: Bearer $TRANSCRIBE_API_KEY"
```

### Teste 4 — Fluxo completo no SaaS

1. Enviar áudio de teste pelo WPP → verificar se chatbot responde com base na transcrição
2. Gravar atendimento curto (30s) → verificar se sumarização aparece
3. Verificar logs do backend: `job_id`, tempo de processamento, erros

---

## 11. Notas importantes

- A API processa **1 áudio por vez** na fila (VPS com 1 vCPU). Se `queue_pending` > 0, a espera aumenta.
- A **primeira** transcrição após deploy pode demorar mais (download do modelo Whisper).
- A API **não armazena** áudio nem texto no banco (configuração LGPD atual). O SaaS é responsável por persistir o que precisar.
- Formato de áudio do WhatsApp costuma ser `.ogg` (Opus). Formato do navegador costuma ser `.webm` ou `.wav`. Ambos são suportados.
- O campo `text` só vem preenchido quando `status === "completed"`.

---

## 12. Resumo para colar no prompt da IA do SaaS

```
Integre a API de transcrição descrita neste documento no backend do SaaS.

URL: https://transcribe.viaprove.com.br
Auth: Bearer token via env TRANSCRIBE_API_KEY (nunca no frontend)

Fluxo: POST /v1/transcribe → polling GET /v1/jobs/{id} a cada 3s → usar text quando completed.

Integrar em:
1. Webhook de áudio do WhatsApp (source=whatsapp, background job)
2. Gravação de atendimento (source=recording, timeout 45min, background job para UX)

Após obter text, passar para o serviço de IA existente (chatbot ou sumarização).
Criar módulo isolado transcribe-api.ts com createJob, getJob, transcribeAndWait.
Adicionar variáveis no .env.example. Tratar erros e timeouts.
```
