
# Monitor de Ofertas — arquivos prontos

Arquivos incluídos:
- `monitor.js` — tempo real via Bot API (bot precisa ser **admin** do canal). Cooldown por **produto + marca + fonte** e ignora se **preço cair** ou variar **≥ 5%**.
- `realtime.py` — tempo real via **Telethon** (não precisa admin). Mesmas regras de cooldown.

## Como usar

1) Copie os arquivos para a pasta do projeto:
   ```bash
   cd ~/Downloads/telegram-offers-monitor
   ```
   Substitua os arquivos existentes (se houver).

2) Confirme que existe um `.env` com:
   ```
   TELEGRAM_TOKEN=SEU_TOKEN_DO_BOT
   TELEGRAM_API_ID=35569911
   TELEGRAM_API_HASH=78d9be33090772f2fd242ede833946ca
   TELEGRAM_PHONE=+5574999647586
   ```

3) Confirme o `config.json` com seus canais e o seu `user_chat_id`:
   ```json
   {
     "channels": ["@canalandrwss", "@TalkPC", "..."],
     "products": [ ... ],
     "user_chat_id": 1818469361
   }
   ```

### Rodar em tempo real (Bot API)
> Use quando o bot for **admin** do seu canal.

```bash
npm start
```

### Rodar em tempo real (Telethon, sem admin — qualquer canal público)
> Recomendado para ouvir canais de terceiros.

```bash
python3 -m pip install telethon python-dotenv requests
python3 realtime.py
```

### Teste rápido
- DM com o bot: `Ryzen 7 5700X por R$ 890` → deve chegar alerta.
- Canal: `RTX 5060 Inno3D R$ 1700` depois `RTX 5060 Inno3D R$ 1500` → deve alertar de novo (preço caiu).
- Canal: `RTX 5060 Galax R$ 1730` → deve alertar (marca diferente).

---

Se precisar, posso ajustar os limites de cooldown, marcas monitoradas ou adicionar logs extras.
