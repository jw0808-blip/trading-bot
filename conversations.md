# TraderJoes AI Conversation Log

All conversations with Grok and Claude in the "TraderJoes Trading Firm" project are automatically saved here.

---

## How to Log Conversations

**Option 1  Discord command:** `!log <message>` in any channel  
**Option 2  Webhook POST:**
```
POST https://ai-logger.onrender.com/log
{"source":"Claude","author":"TraderJoe","content":"...","secret":"traderjoes2024"}
```
**Option 3  Log bot activity:**
```
POST https://ai-logger.onrender.com/log/bot
{"bot_name":"Bot1","action":"BUY","details":"...","secret":"traderjoes2024"}
```

---

