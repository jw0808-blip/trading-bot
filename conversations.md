# TraderJoes AI Conversation Log

All conversations with Grok and Claude in the "TraderJoes Trading Firm" project are automatically saved here.

---

## How to Log Conversations

**Discord command:** `!log <your message>` in any channel  
**Webhook:** `POST https://<ai-logger>.onrender.com/log`

```json
{
  "source": "Claude",
  "author": "TraderJoe", 
  "content": "Your conversation text here...",
  "secret": "traderjoes2024"
}
```

---

