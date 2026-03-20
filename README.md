# StreamerAssistant

**StreamerAssistant** is a powerful, locally-hosted unified chat, alert, and moderation relay daemon. It seamlessly integrates Twitch, Kick, and YouTube chat events into a single standardized platform, perfect for custom overlays, SAMMI integration, and AI-powered chat moderation.

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

## 🌟 Key Features

*   **Multi-Platform Unification**: Reads and normalizes chat events, redemptions, subscriptions, and alerts from Twitch, Kick, and YouTube without requiring complex user logins for basic read-only monitoring.
*   **3-Tier AI Moderation Engine**:
    1.  **Regex Fast-Pass**: Instantly catches obvious spam, links, or IP addresses.
    2.  **VADER Sentiment Analysis**: Offline, fast, English-based heuristic sentiment checking.
    3.  **Local LLM (via Ollama)**: Deep context, multi-lingual analysis that detects toxic spam or hate speech, acting as the ultimate safety net.
*   **Local Text-to-Speech (TTS)**: Built-in integration with Piper TTS, dynamically downloading Hugging Face voice weights for offline, high-quality speech synthesis.
*   **Broadcaster Integrations**: Out-of-the-box support for **SAMMI** via HTTP POST Webhooks and real-time **WebSockets** for OBS HTML overlays.
*   **Dynamic Soft-Reloading**: Change configurations on the fly via the built-in Web UI. The application gracefully restarts its listener queues without killing the host process.
*   **System Tray Integration**: Runs cleanly in the background with a system tray icon for easy access.

## 🚀 Getting Started

### Prerequisites

*   **Python 3.10+**
*   (Optional but recommended) [Ollama](https://ollama.com/) installed locally for deep-context AI moderation.
*   (Optional) If you plan to compile it to an EXE, you will need `pyinstaller`.

### Installation

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/yourusername/StreamerAssistant.git
    cd StreamerAssistant
    ```

2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

    *Note: The script handles necessary downloads for the Piper TTS engine automatically.*

3.  **Run the application**:
    ```bash
    python StreamerAssistant.py
    ```

### Configuration

When you first run StreamerAssistant, it will use/generate a `config.json` file. A `config.example.json` is provided in the repository to demonstrate the structure.
To configure the application:
1. Run the script. A system tray icon will appear.
2. Right-click the system tray icon and open the **Settings** or **Console** interface (or open the local `.html` files in your browser).
3. Add your channels (zones) to monitor, configure moderation aggressiveness, and setup your SAMMI/Webhook endpoints.

## 🧠 Architecture Setup & Data Flow

StreamerAssistant acts as a central hub:
1.  **Input**: Connects to platforms (Twitch, Kick, YouTube) via WebSocket/API protocols. generates uniform JSON payloads.
2.  **Processing**: Passes messages through the 3-Tier Moderation queue.
3.  **Output**: 
    *   Broadcasts safe messages to the Chat Overlay via local WebSockets (`ws://127.0.0.1:41837`).
    *   Sends triggers and moderation events natively to SAMMI via webhook (`http://127.0.0.1:9450/webhook`).
    *   Queues safe messages for the Piper TTS engine.

For developers looking to hook into this system (e.g., building custom plugins or overlays), check out the [StreamerAssistant Developer Guide](StreamerAssistant_Developer_Guide.md) for full WebSocket and HTTP Webhook API specifications.

## 🖥️ Web Interfaces & Overlays

StreamerAssistant includes several ready-to-use local HTML interfaces. You can open these directly in your browser or add them as Browser Sources in OBS:

*   **`settings.html`**: The primary control panel. Use this to configure your platform zones (Twitch, Kick, YouTube), tweak AI moderation aggressiveness, setup TTS voices, and define your SAMMI webhook credentials.
*   **`console.html`**: A live developer console that connects to the background daemon. Great for debugging, viewing incoming chat payloads in real-time, and monitoring system statuses or errors.
*   **`chat_overlay.html`**: A clean, unified chat overlay designed to be added as an OBS Browser Source. It listens to the local WebSocket (Port `41837`) and visually displays messages that have passed moderation.
*   **`obs_bridge.html` / `warudo_bridge.html`**: Integration endpoints designed to bridge the gap between StreamerAssistant's standard events and software-specific local APIs (like obs-websocket or Warudo's internal routing).

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!
Feel free to check out the [issues page](https://github.com/yourusername/StreamerAssistant/issues).

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.

## 🙏 Acknowledgements

A huge thank you to the creators of the underlying libraries that make this project possible:
* [aio-libs/aiohttp](https://github.com/aio-libs/aiohttp)
* [Ollama](https://ollama.com)
* [Pillow](https://python-pillow.org/)
* [pytchat](https://github.com/taizan-hokuto/pytchat)
* [vaderSentiment](https://github.com/cjhutto/vaderSentiment)
* [websockets](https://github.com/aaugustin/websockets)
