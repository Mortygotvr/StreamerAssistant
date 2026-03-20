================================================================================
STREAMER ASSISTANT - API & PLUGIN DEVELOPMENT REFERENCE
================================================================================

This document provides complete technical specifications for building custom overlays, automated plugins, and third-party integrations that interface with the StreamerAssistant ecosystem.

--------------------------------------------------------------------------------
1. ARCHITECTURE OVERVIEW
--------------------------------------------------------------------------------

StreamerAssistant acts as a centralized local daemon that normalizes events across Twitch, Kick, and YouTube.
It exposes its data via two primary channels:
  A. Local WebSocket Server (Port 41837) - Real-time pub/sub for UI and plugins.
  B. Outbound HTTP POST Webhooks (Port 9450) - Delivered specifically to SAMMI.

--------------------------------------------------------------------------------
2. WEBSOCKET API SPECIFICATION
--------------------------------------------------------------------------------

Connection URI : ws://127.0.0.1:41837
Protocol       : JSON over WebSocket
Retry Logic    : Clients should implement a 2000-5000ms reconnect backoff.

>>> A. INBOUND MESSAGES (Client -> StreamerAssistant)
Send JSON payloads to the socket to control the backend or request data.

1. Test Event Simulation
   Forces the backend to simulate a platform event, triggering Hotkeys, OBS, TTS, SAMMI, and rebroadcasting to all WebSocket clients.
```json
   {
       "type": "test_sammi_trigger",
       "trigger": "Twitch sub",
       "user": "Strimer",
       "var": "500"
   }
```

2. Update Configuration
   Overwrites the current JSON config state and re-initializes listeners.
```json
   {
       "type": "save_config",
       "payload": { /* ... full config object ... */ }
   }
```

3. TTS Queue Controls
```json
   { "type": "set_volume", "volume": 0.8 }
   { "type": "set_device", "device_id": "{0.0.0.0...}" }
   { "type": "test_voice" }
   { "type": "stop_tts" }
```

>>> B. OUTBOUND MESSAGES (StreamerAssistant -> Client)
Listen to the socket to react to live chat events or system state changes.

1. Chat / Alert / Moderation Pipeline Payload
   Fired whenever a normalized chat, subscription, raid, etc. occurs.
```json
   {
       "type": "moderation",
       "payload": {
           "trigger": "Twitch sub",
           "msg_id": "dceb1a99-...",
           "platform": "Twitch",
           "username": "User123",
           "message": "I just subscribed!",
           "customData": {
               "amount": 1,
               "tier": "1000"
           },
           "vader_score": 0.943,
           "ai_reason": "CLEAN"
       }
   }
```

   ** Trigger Naming Standard:
   Format: "[Platform] [Action]"
   Common Actions: "chat", "sub", "cheer", "raid", "follow", "redeem"

--------------------------------------------------------------------------------
3. SAMMI WEBHOOK SPECIFICATION
--------------------------------------------------------------------------------

Method: HTTP POST
Target: http://127.0.0.1:9450/webhook (configurable)
Header: Content-Type: application/json

Payload Body Structure:
Exactly matches the "payload" object from the WebSocket "moderation" broadcast.

Building in SAMMI:
1. Create a Webhook Trigger pointing to local SAMMI webhook.
2. The trigger string must EXACTLY match the "trigger" attribute.
   (e.g., Trigger name in SAMMI = "Twitch sub").
3. Use SAMMI's variable parsing to pull nested JSON data:
   - msg_id & username & message
   - customData.amount

--------------------------------------------------------------------------------
4. PLUGIN DEVELOPMENT EXAMPLES
--------------------------------------------------------------------------------

>>> JavaScript Plugin Example
```javascript
const ws = new WebSocket("ws://127.0.0.1:41837");

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    if (data.type === "moderation") {
        const payload = data.payload;
        console.log(`[${payload.platform}] ${payload.username} triggered ${payload.trigger}`);
        
        if (payload.trigger === "Twitch sub") {
            // Do visual animation
            console.log("Sub received!");
        }
    }
};
```

>>> Python Plugin Example
```python
import asyncio
import websockets
import json

async def plugin_listener():
    async with websockets.connect("ws://127.0.0.1:41837") as ws:
        async for message in ws:
            event = json.loads(message)
            if event.get("type") == "moderation":
                print(f"New Event: {event['payload']['trigger']}")

asyncio.run(plugin_listener())
```
