# 🏛️ Multi-Agent AI Telegram Group Chat Council

An institutional multi-agent system powered by **Microsoft AutoGen** (`autogen-agentchat`), where 5 to 15 distinct AI models (Claude, GPT, DeepSeek, Gemini, Grok, and more) communicate with each other directly inside a **Telegram Group Chat**. The AI agents debate complex propositions, critique each other's points, hold formal consensus votes, and automatically execute trades or on-chain tasks via the **Market Sniffer (`@Msnifferbot`)** crypto bot upon reaching majority approval.

---

## 🌟 Key Capabilities

1. **Dedicated Telegram Bot Identities for Each AI Agent**:
   - Every AI model is bound to its own Telegram bot token.
   - When an agent speaks during their AutoGen turn, the message is dispatched to the Telegram group chat using **that specific agent's bot token** and avatar.
   - Visually, the bots talk, debate, and vote with each other in real-time in the group chat.

2. **Autonomous Display Name Synchronization (`setMyName`)**:
   - Built-in startup utility automatically queries the Telegram Bot API (`setMyName`) to ensure each bot displays the exact `{AI}talks` naming pattern (e.g., `Claudetalks`, `GPTtalks`, `DeepSeektalks`, `Geminitalks`, `Groktalks`).

3. **4 Operational Modes & Clean Shorthands**:
   - `/deb <prompt>`: Multi-turn debate mode across agents.
   - `/vot <prompt>`: Formal structured voting with reasoning and automated consensus tallying.
   - `/exe <prompt>`: Vote & Execute mode—triggers the external MSniffer crypto bot if majority approval is reached.
   - `/dve <prompt>`: Full pipeline (Debate ➔ Vote ➔ Execute).
   - `/mood <mood_type>`: Dynamically adjusts council personality (e.g. `skeptical`, `aggressive`, `conservative`, `ruthless`, `analytical`).

4. **External Task Execution Bridge (`executor.py`)**:
   - Connects directly to **Market Sniffer (`market_sniffer_bot` / `@Msnifferbot`)**.
   - Extracts Solana Token Mint Addresses (CAs), trade action (BUY/SELL), and amount in SOL.
   - Dispatches via **HTTP Webhook** (`POST /trade`), **Telegram command** (`/signal BUY <mint> <amount>`), or **Local IPC**.

5. **Flexible LLM Backend Architecture**:
   - Supports a single unified **OpenRouter** API key (one key powers Claude 3.5 Sonnet, GPT-4o, DeepSeek, Gemini 2.5 Flash, and Grok 2 simultaneously with zero SDK conflicts).
   - Also supports native provider keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `GROK_API_KEY`, `MISTRAL_API_KEY`).
   - Includes intelligent simulated fallback mode if keys have not been added yet.

6. **Modular Scalability (5 to 15 Bots)**:
   - Configured with 5 initial bots out of the box.
   - Simply add tokens to `.env` to enable slots 6 through 15 (Llama 3.3, Qwen 2.5, Cohere, Perplexity, DBRX, Mixtral, etc.).

---

## 🏗️ Architecture Overview

```
                      ┌────────────────────────────────────────┐
                      │          Admin (You) in Group          │
                      └──────────────────┬─────────────────────┘
                                         │ Telegram Commands
                                         │ (/deb, /vot, /exe, /dve)
                                         ▼
                      ┌────────────────────────────────────────┐
                      │          Listener Bot Engine           │
                      │         (claudetalkbbot Poller)        │
                      └──────────────────┬─────────────────────┘
                                         │ Dispatches Task
                                         ▼
                      ┌────────────────────────────────────────┐
                      │       Microsoft AutoGen Council        │
                      │  (RoundRobinGroupChat & Turn-Taking)   │
                      └────┬───────┬───────┬───────┬───────┬───┘
                           │       │       │       │       │
             ┌─────────────┘       │       │       │       └─────────────┐
             ▼                     ▼       ▼       ▼                     ▼
      [Claudetalks]           [GPTtalks] [...] [Geminitalks]       [Groktalks]
             │                     │       │       │                     │
             │ (Broadcast via      │       │       │                     │
             │  bot's own token)   │       │       │                     │
             ▼                     ▼       ▼       ▼                     ▼
      ┌──────────────────────────────────────────────────────────────────┐
      │                   Telegram Group Chat Window                     │
      │   (Bots debate, vote YES/NO, and tally consensus in real-time)   │
      └──────────────────────────────────┬───────────────────────────────┘
                                         │ Consensus Passed (>50% YES)
                                         ▼
                      ┌────────────────────────────────────────┐
                      │          executor.py Dispatch          │
                      │     (Webhook / Telegram / Local IPC)   │
                      └──────────────────┬─────────────────────┘
                                         │
                                         ▼
                      ┌────────────────────────────────────────┐
                      │       Market Sniffer Crypto Bot        │
                      │       (@Msnifferbot / Jupiter DEX)     │
                      │       Executes Solana Buy/Sell Tx      │
                      └────────────────────────────────────────┘
```

---

## 🤖 Bot Tokens & Agent Lineup

| # | Agent Name | Display Name | Telegram Username | Dedicated Token | Role & Domain |
|---|------------|--------------|-------------------|-----------------|---------------|
| 1 | **Claude** | `Claudetalks` | `@claudetalkbbot` | `8828456879:AAHqJUXEuCK4W--r4t3brAR30mn7sThLMLY` | Security & Smart Contract Auditor |
| 2 | **GPT** | `GPTtalks` | `@gpttalkbbot` | `8630079807:AAEhjG1JY8RzayPeI3hLfc5Dixmvh7eJcUs` | Macro Strategist & Liquidity Analyst |
| 3 | **DeepSeek** | `DeepSeektalks` | `@deepseektalkbbot` | `8882002776:AAGdh55_7Ct6RJ3PGDZk_wEAsTnZIrOz0fY` | Quant & Mathematical Probability |
| 4 | **Gemini** | `Geminitalks` | `@geminitalkbbot` | `8934071377:AAEXDzWKMLjpDkPlxztbmwzPKqRge8vSegk` | Trend Velocity & Ecosystem Synthesizer |
| 5 | **Grok** | `Groktalks` | `@groktalkbbot` | `8607678038:AAHxLLtGoL6gEgnJoN35toGkKPbWCYC8VHI` | High-Momentum Alpha & Degen Sentiment |

*(Slots 6-15 are defined in `config.py` for Mistral, Llama 3.3, Qwen 2.5, Cohere, Perplexity, DBRX, Haiku, MiniGPT, GrokMini, and Mixtral).*

---

## 🛠️ Step-by-Step Setup Guide

### 1. Telegram Group Configuration & Group Privacy

For the bots to participate seamlessly in your Telegram group chat, complete these 3 quick steps:

#### A. Disable Group Privacy in `@BotFather` (CRITICAL)
By default, Telegram bots operate in "Privacy Mode", meaning they cannot see messages from regular users in a group unless mentioned or starting with `/`. 

To allow the listener bot to process group interactions properly:
1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send the command: `/mybots`
3. Select your listener bot (e.g., `@claudetalkbbot`).
4. Click **Bot Settings** ➔ **Group Privacy** ➔ **Turn off**.
5. You will see the confirmation: *`Privacy mode is now disabled for @claudetalkbbot`*.
*(Repeat for the other 4 bots if you wish for them to also receive direct mentions).*

#### B. Create the Group and Add the Bots
1. In Telegram, create a new Group Chat (e.g., `"AI Alpha Council"`).
2. Add all 5 bots to the group:
   - `@claudetalkbbot`
   - `@gpttalkbbot`
   - `@deepseektalkbbot`
   - `@geminitalkbbot`
   - `@groktalkbbot`
3. *(Recommended)* Promote `@claudetalkbbot` (or all 5 bots) to **Administrator** with permission to send messages.

#### C. Get Your `GROUP_CHAT_ID`
You can get your group chat ID in either of two ways:
- **Automatic Method**: Simply leave `GROUP_CHAT_ID=0` in `.env`. When you type `/start` in the group chat, the bot will detect and log your `GROUP_CHAT_ID` in the terminal!
- **Manual Method**: Add `@RawDataBot` to the group chat. It will output a JSON message containing `"chat": {"id": -100xxxxxxxxxx}}`. Copy that ID and remove `@RawDataBot`.

---

### 2. Configure LLM API Keys in `.env`

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

#### Option A: Unified OpenRouter Key (Recommended ⭐)
OpenRouter provides a unified OpenAI-compatible endpoint that routes to Claude 3.5 Sonnet, GPT-4o, DeepSeek V3/R1, Gemini 2.5 Flash, and Grok 2 under **a single API key and billing balance**:
1. Sign up at [https://openrouter.ai](https://openrouter.ai).
2. Generate an API key under [https://openrouter.ai/keys](https://openrouter.ai/keys).
3. Paste it into `.env`:
   ```env
   OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxxxxxx
   ```

#### Option B: Individual Provider API Keys
If you already possess separate provider keys, you can specify them individually in `.env`:
```env
OPENAI_API_KEY=sk-proj-xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
DEEPSEEK_API_KEY=sk-xxxxxxxx
GEMINI_API_KEY=AIzaxxxxxxxx
GROK_API_KEY=xai-xxxxxxxx
MISTRAL_API_KEY=xxxxxxxx
```

---

### 3. Connect to Market Sniffer (`@Msnifferbot`)

The council's `/exe` and `/dve` commands automatically execute trades or audits against Market Sniffer.

In your `.env`:
```env
# Choose your dispatch mode: "webhook", "telegram", "local_ipc", or "all"
MSNIFFER_DISPATCH_MODE=webhook

# 1. HTTP Webhook (Co-located with market_sniffer_bot)
MSNIFFER_WEBHOOK_URL=http://127.0.0.1:8080/trade
MSNIFFER_SECRET=your_secret_from_market_sniffer

# 2. Telegram Signal Dispatch (Dispatches /signal to @Msnifferbot)
MSNIFFER_CHAT_ID=@Msnifferbot

# Trade Defaults
DEFAULT_TRADE_AMOUNT_SOL=0.25
DEFAULT_SLIPPAGE_BPS=300
```

When a consensus vote passes (>50% YES):
1. `SignalExtractor` extracts the Solana token address (e.g. `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263`) and amount from the proposition.
2. `MSnifferExecutor` dispatches the order to Market Sniffer.
3. Market Sniffer executes the Jupiter swap on Solana mainnet and returns the Solscan transaction link.
4. The council bot announces the result in the group chat.

---

## ⚡ Telegram Commands & Shorthands

Type these commands directly inside your Telegram group chat:

### 1. `/deb <prompt>` — Multi-Turn Debate Mode
Agents debate the topic from their respective domain angles without voting or executing.
```text
/deb Should we allocate 0.5 SOL into the new pump.fun AI meme narrative?
```
- **Claudetalks**: Evaluates code security, token freeze authority, and LP lock status.
- **GPTtalks**: Reviews macro DEX liquidity and volume rotation.
- **DeepSeektalks**: Calculates volume acceleration and net buy/sell ratio.
- **Geminitalks**: Gauges social velocity and narrative momentum.
- **Groktalks**: Evaluates PvP trader positioning and asymmetric upside.

---

### 2. `/vot <prompt>` — Formal Vote Mode
Each agent presents a 2-3 sentence verdict and casts a structured vote (`VOTE: YES` or `VOTE: NO`). The system tallies votes and declares whether consensus passed.
```text
/vot Approve buying $BONK at current liquidity levels?
```
**Sample Output:**
```
📊 COUNCIL VOTE RESULTS
───────────────────────────────
📌 Proposition: Approve buying $BONK at current liquidity levels?

✅ Claude: YES
✅ GPT: YES
❌ DeepSeek: NO
✅ Gemini: YES
✅ Grok: YES

───────────────────────────────
✅ YES: 4  |  ❌ NO: 1  |  ⚪ ABSTAIN: 0
📈 Consensus: 80.0% (Threshold: 50%)
🏆 STATUS: CONSENSUS REACHED (APPROVED)
───────────────────────────────
```

---

### 3. `/exe <prompt>` — Vote & Execute Mode
Takes a formal vote on a trade proposal. If majority consensus passes, the MSniffer execution bridge fires immediately.
```text
/exe Buy 0.25 SOL of DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
```

---

### 4. `/dve <prompt>` — Full Pipeline (Debate ➔ Vote ➔ Execute)
The full multi-agent institutional pipeline:
1. **Debate Phase**: Council debates pros and cons across multiple rounds.
2. **Voting Phase**: Each agent casts an official structured vote.
3. **Execution Phase**: If majority approval is secured, the trade signal is dispatched automatically to Market Sniffer!
```text
/dve Evaluate DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 and buy 0.25 SOL if approved
```

---

### 5. `/mood <mood_type>` — Council Mood & Personality Switcher
Adjusts the collective perspective and debate style:
- `/mood analytical`: Balanced, data-driven, objective metrics (default).
- `/mood skeptical`: Hyper-vigilant, actively seeks out honeypots and rug-pulls.
- `/mood aggressive`: High-conviction, degen momentum, breakout seeking.
- `/mood conservative`: Capital preservation first, $50K+ liquidity floor.
- `/mood ruthless`: Cold, cutthroat, PvP game theory.
- `/mood contrarian`: Fades popular crowd sentiment.

---

### 6. Administrative & Utility Commands
- `/status`: View active council lineup, current mood, group ID, and LLM backend.
- `/setnames`: Triggers `setMyName` to re-sync all 5 bot display names to `{AI}talks`.
- `/help`: Detailed command cheat sheet.
- `/cancel` or `/stop`: Abort any currently running debate or vote.

---

## 🚀 How to Run

### 1. Install Dependencies
```bash
cd /home/lazorr/multi_agent_talks
pip install -r requirements.txt
```

### 2. Verify or Update `.env`
Ensure your bot tokens and preferred LLM API keys are populated in `.env`.

### 3. Start the System
```bash
python3 main.py
```

On startup, you will observe the startup banner and display name synchronization:
```text
=================================================================
🏛️ MULTI-AGENT TELEGRAM GROUP CHAT SYSTEM STARTING
=================================================================
📡 Listener / Poller Bot : Claude (@claudetalkbbot)
👥 Active Council Agents : 5
📍 Target Group Chat ID : Auto-detect on first message
🐕 MSniffer Dispatch Mode: WEBHOOK
=================================================================

[Claude] @claudetalkbbot -> Display: 'Claudetalks' (already matches)
[GPT] @gpttalkbbot -> Display: 'GPTtalks' (already matches)
[DeepSeek] @deepseektalkbbot -> Display: 'DeepSeektalks' (already matches)
[Gemini] @geminitalkbbot -> Display: 'Geminitalks' (already matches)
[Grok] @groktalkbbot -> Display: 'Groktalks' (already matches)
🏛️ Council Orchestrator ready with 5 AI members.
Starting long polling on @claudetalkbbot...
```

---

## 📈 Expanding Up to 15 AI Bots

The system is pre-configured with 10 additional expandable slots in `config.py` (Bots 6 through 15).

To add an agent:
1. Create a new bot in Telegram via **[@BotFather](https://t.me/BotFather)** (e.g., `mistraltalkbbot`).
2. Add its token to your `.env`:
   ```env
   BOT_TOKEN_MISTRAL=1234567890:ABCdefGhIJKlmNoPQRstuvwxYZ
   ```
3. Add the bot to your Telegram group chat.
4. Restart `main.py` (or run `/setnames`). The bot will automatically appear in council debates and votes!

---

## 📁 Project Structure

```
multi_agent_talks/
├── main.py              # Telegram Bot application entrypoint, startup sync & command handlers
├── agents.py            # Microsoft AutoGen orchestration, multi-bot Telegram broadcasting & voting
├── config.py            # Bot tokens (up to 15), OpenRouter/native LLM endpoints & mood prompts
├── executor.py          # Market Sniffer bridge (Webhook, Telegram, Local IPC) & CA extractor
├── requirements.txt     # Python package dependencies
├── .env.example         # Documented environment variables template
├── .env                 # Live environment configuration
└── README.md            # Comprehensive documentation & setup instructions
```

---

## 🛡️ Security & Operational Notes

- **Admin Whitelist**: Set `ADMIN_USER_IDS` in `.env` to restrict execution commands exclusively to authorized Telegram IDs.
- **Telegram Limits**: Telegram enforces a 4,096 character limit per message. `agents.py` automatically chunks oversized responses at natural paragraph boundaries and falls back to plain text if markdown formatting fails.
- **Visual Realism**: Configurable `BOT_TYPING_DELAY_SECONDS=1.5` triggers Telegram's `"typing..."` action before each bot speaks, creating an authentic human-like conversation flow.
