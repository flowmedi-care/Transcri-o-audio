# Deploy na VPS — passo a passo

Guia para subir a API de transcrição na sua VPS Hostinger (Ubuntu 24.04).

**Substitua em todo este guia:**

| Placeholder | Seu valor |
|-------------|-----------|
| `SEUDOMINIO.com` | Seu domínio real (ex: `flowmedi.care`) |
| `2.24.88.155` | IP da sua VPS (já é o seu) |
| `SUA_API_KEY` | Chave longa e aleatória que você gera |

---

## O que vamos fazer

1. Conectar na VPS via SSH
2. Instalar dependências (Python, ffmpeg, Nginx)
3. Clonar o projeto
4. Configurar variáveis de ambiente
5. Rodar a API como serviço (systemd)
6. Apontar subdomínio `transcribe.SEUDOMINIO.com` para a VPS
7. Ativar HTTPS com Certbot

---

## Pré-requisitos

- Acesso SSH à VPS (usuário `root`, IP `2.24.88.155`)
- Domínio com painel DNS
- Projeto Supabase criado
- Repositório Git com este código (GitHub, GitLab, etc.)

---

## Fase 1 — Primeiro acesso SSH

### Windows (PowerShell ou CMD)

Se ainda não tem chave SSH, use a senha que a Hostinger enviou:

```powershell
ssh root@2.24.88.155
```

Na primeira conexão, digite `yes` quando perguntar sobre fingerprint.

### O que é SSH?

É um terminal remoto na sua VPS. Tudo que você digitar depois de conectar roda **no servidor Linux**, não no seu PC.

---

## Fase 2 — Atualizar o sistema

Já conectado na VPS:

```bash
apt update && apt upgrade -y
```

| Comando | O que faz |
|---------|-----------|
| `apt update` | Atualiza lista de pacotes |
| `apt upgrade -y` | Instala atualizações de segurança (`-y` = sim automático) |

---

## Fase 3 — Criar usuário `deploy`

Não use `root` no dia a dia:

```bash
adduser deploy
```

Siga as perguntas (senha opcional se usar só chave SSH).

```bash
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/ 2>/dev/null || true
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

---

## Fase 4 — Firewall

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status
```

Libera: SSH (22), HTTP (80), HTTPS (443).

---

## Fase 5 — Instalar dependências

```bash
apt install -y python3 python3-venv python3-pip ffmpeg git nginx certbot python3-certbot-nginx
```

| Pacote | Para quê |
|--------|----------|
| `python3-venv` | Ambiente virtual Python |
| `ffmpeg` | Converter áudios (WPP, navegador) |
| `nginx` | Reverse proxy (domínio → API) |
| `certbot` | Certificado HTTPS gratuito |

Verifique:

```bash
ffmpeg -version
python3 --version
```

---

## Fase 6 — Clonar o projeto

```bash
mkdir -p /opt/transcribe-api
chown deploy:deploy /opt/transcribe-api
su - deploy
cd /opt/transcribe-api
```

Clone seu repositório (substitua a URL):

```bash
git clone https://github.com/SEU_USUARIO/transcribe-api.git .
```

Se o código ainda não está no Git, você pode enviar via `scp` do seu PC:

```powershell
# No seu PC (PowerShell), na pasta do projeto:
scp -r ".\*" deploy@2.24.88.155:/opt/transcribe-api/
```

---

## Fase 7 — Ambiente Python

Ainda como usuário `deploy` em `/opt/transcribe-api`:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

A primeira transcrição baixa o modelo Whisper (~500 MB para `small`). Isso é normal.

---

## Fase 8 — Configurar `.env`

```bash
cp .env.example .env
nano .env
```

Edite os valores principais:

```env
API_KEY=cole-uma-chave-longa-aleatoria-aqui
HOST=127.0.0.1
PORT=8000

WHISPER_MODEL=small
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=pt

SAVE_AUDIO=false
SAVE_TRANSCRIPT=false
SAVE_METRICS=true

SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=sua-service-role-key
```

Salvar no nano: `Ctrl+O`, Enter, `Ctrl+X`.

**Gerar API key no Linux:**

```bash
openssl rand -hex 32
```

---

## Fase 9 — Supabase

1. Acesse [supabase.com](https://supabase.com) → seu projeto
2. Vá em **SQL Editor**
3. Cole o conteúdo de `supabase/migrations/001_transcription_jobs.sql`
4. Clique **Run**

Pegue as credenciais em **Project Settings → API**:

- `SUPABASE_URL` = Project URL
- `SUPABASE_SERVICE_KEY` = `service_role` (secret — só no servidor)

---

## Fase 10 — Teste manual (antes do systemd)

```bash
source /opt/transcribe-api/.venv/bin/activate
cd /opt/transcribe-api
python -m app.main
```

Em outro terminal SSH:

```bash
curl http://127.0.0.1:8000/health
```

Deve retornar JSON com `"status": "ok"`. Pare o servidor com `Ctrl+C`.

---

## Fase 11 — Serviço systemd (inicia sozinho)

Volte para root:

```bash
exit   # sai do usuário deploy, volta para root
```

```bash
cp /opt/transcribe-api/deploy/transcribe-api.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable transcribe-api
systemctl start transcribe-api
systemctl status transcribe-api
```

Deve mostrar `active (running)` em verde.

**Comandos úteis:**

```bash
systemctl restart transcribe-api    # reiniciar
systemctl stop transcribe-api       # parar
journalctl -u transcribe-api -f     # ver logs ao vivo
journalctl -u transcribe-api -n 100 # últimas 100 linhas
```

---

## Fase 12 — DNS (subdomínio)

No painel DNS do seu domínio (Hostinger, Cloudflare, etc.):

| Tipo | Nome | Valor | TTL |
|------|------|-------|-----|
| A | `transcribe` | `2.24.88.155` | 300 (ou Auto) |

Resultado: `transcribe.SEUDOMINIO.com` → sua VPS.

Propagação: de 5 minutos a algumas horas. Teste:

```bash
ping transcribe.SEUDOMINIO.com
```

O IP deve ser `2.24.88.155`.

---

## Fase 13 — Nginx

Substitua `flowmedi.care` pelo seu domínio real:

```bash
sed 's/SEUDOMINIO.com/flowmedi.care/g' /opt/transcribe-api/deploy/nginx-transcribe.conf > /etc/nginx/sites-available/transcribe
```

Ou edite manualmente:

Ative o site:

```bash
ln -s /etc/nginx/sites-available/transcribe /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

Teste HTTP:

```bash
curl http://transcribe.SEUDOMINIO.com/health
```

---

## Fase 14 — HTTPS (Certbot)

```bash
certbot --nginx -d transcribe.SEUDOMINIO.com
```

Siga as perguntas:

- Email: seu email
- Aceitar termos: sim
- Redirect HTTP → HTTPS: sim (recomendado)

Teste HTTPS:

```bash
curl https://transcribe.SEUDOMINIO.com/health
```

O Certbot renova automaticamente. Teste renovação:

```bash
certbot renew --dry-run
```

---

## Fase 15 — Teste completo com áudio

```bash
curl -X POST "https://transcribe.SEUDOMINIO.com/v1/transcribe" \
  -H "Authorization: Bearer SUA_API_KEY" \
  -F "file=@/caminho/teste.ogg" \
  -F "user_id=teste_1" \
  -F "source=whatsapp"
```

Copie o `job_id` e consulte:

```bash
curl "https://transcribe.SEUDOMINIO.com/v1/jobs/JOB_ID_AQUI" \
  -H "Authorization: Bearer SUA_API_KEY"
```

Repita até `status` ser `completed`.

---

## Configurar no seu SaaS

No backend do SaaS, adicione variáveis de ambiente:

```env
TRANSCRIBE_API_URL=https://transcribe.SEUDOMINIO.com
TRANSCRIBE_API_KEY=SUA_API_KEY
```

Use os exemplos em [API.md](API.md) para integrar.

---

## Atualizar o app depois

```bash
su - deploy
cd /opt/transcribe-api
git pull
source .venv/bin/activate
pip install -r requirements.txt
exit
sudo systemctl restart transcribe-api
```

---

## Troubleshooting

### Serviço não sobe (`systemctl status` mostra failed)

```bash
journalctl -u transcribe-api -n 50 --no-pager
```

Causas comuns:

- `.env` com `API_KEY` vazio
- Erro de sintaxe no `.env`
- Permissão: `/opt/transcribe-api` deve pertencer a `deploy`

### 502 Bad Gateway no Nginx

A API não está rodando:

```bash
systemctl status transcribe-api
curl http://127.0.0.1:8000/health
```

### ffmpeg não encontrado

```bash
which ffmpeg
apt install -y ffmpeg
systemctl restart transcribe-api
```

### Sem espaço em disco

```bash
df -h
du -sh /opt/transcribe-api/data
du -sh /home/deploy/.cache
```

Limpe temporários:

```bash
rm -rf /opt/transcribe-api/data/tmp/*
```

### Transcrição muito lenta

Normal com 1 vCPU. Monitore:

```bash
curl https://transcribe.SEUDOMINIO.com/health
```

Se `queue_pending` cresce sempre, considere upgrade da VPS.

### Erro Supabase

- Confirme `SUPABASE_URL` e `SUPABASE_SERVICE_KEY`
- Confirme que a migration SQL foi executada
- Com `SAVE_METRICS=false`, erros de Supabase são ignorados

---

## Checklist final de deploy

- [ ] SSH funciona
- [ ] `ffmpeg` instalado
- [ ] `.env` configurado com `API_KEY` e Supabase
- [ ] Migration SQL executada no Supabase
- [ ] `systemctl status transcribe-api` = active
- [ ] DNS `transcribe` aponta para `2.24.88.155`
- [ ] `curl https://transcribe.SEUDOMINIO.com/health` retorna ok
- [ ] Teste de upload + polling funciona
- [ ] SaaS configurado com `TRANSCRIBE_API_URL` e `TRANSCRIBE_API_KEY`

---

## Quando me passar o subdomínio real

Envie algo como: `transcribe.flowmedi.care`

Aí podemos:

1. Atualizar `deploy/nginx-transcribe.conf` com o domínio exato
2. Revisar juntos o DNS e o primeiro deploy ao vivo
3. Fazer o teste end-to-end com um áudio real do seu SaaS
