# tw-line-faq-bot｜小店 LINE 客服機器人

客人在 LINE 官方帳號問問題，機器人先照你寫好的問答（FAQ）自動回。
**答不出來的，推一張按鈕卡片到老闆的 LINE，老闆點一下，機器人就幫你回客人。**

```
客人 ──▶ LINE 官方帳號 ──▶ bot
                            ├─ 敏感詞（退款、客訴、過敏…）→ 一律轉真人，機器人不亂回
                            ├─ FAQ 有把握 → 直接回（營業時間、價格、外送、付款…）
                            ├─ （選用）Claude 依知識庫補答 → 沒把握就不答
                            └─ 還是答不出來 → 推卡片給老闆：[照原價不折] [可以 95 折] [我自己回]
                                                   老闆點一下 ──▶ bot 幫你回客人
```

- **不一定要用 AI**：預設只用關鍵字比對，行為可預測、不花錢。想要更聰明再填 Claude API key。
- **收訊立刻回 200**：webhook 收到就丟進佇列，背景慢慢處理，不會因為 AI 慢而被 LINE 判逾時。
- **reply／push 自動切換**：LINE 的 reply token 有時效，處理太久會自動改用 push。
- **老闆要認證碼才能綁定**：不會有人打「我是老闆」就拿到管理權。
- 資料（聯絡人、訊息、待辦）存在你電腦的 SQLite，不會上傳到別人的雲端。

> 範例的「示範茶飲」、價格、地址全部是虛構的，請換成你自己的。機器人回覆原則上僅供參考，價格、承諾、退款等事項請以店家本人確認為準。

---

## 適合誰

- 飲料店、餐廳、民宿、美髮美甲、補習班、小型電商：**常見問題一直重複、老闆又常常在忙**的店
- 想先用「零 AI 成本」的關鍵字客服，之後再加 AI 的人
- 想看一個「非同步 webhook + 人工把關 + 選用 AI」的 LINE bot 範例的開發者

---

## 需要哪些金鑰？

| 項目 | 必填？ | 費用 |
|---|---|---|
| LINE Channel secret、Channel access token | ✅ 必填 | 免費（推播則數依官方帳號方案） |
| 老闆認證碼（自己想一組） | ✅ 必填（或改填老闆 userId） | 免費 |
| Anthropic（Claude）API 金鑰 | 選填，AI 補答才需要 | 依用量計費 |

👉 **每一項怎麼申請、填在哪、費用和外流處理，請看 [docs/金鑰申請.md](docs/金鑰申請.md)**（每一步都寫了要點哪個選單，照著做就好）。

---

## 怎麼用（約 20 分鐘，只做一次）

**需要：** Python 3.10 以上（[python.org](https://www.python.org/downloads/)，Windows 安裝時勾選「Add python.exe to PATH」）、一個 LINE 官方帳號。

### 1. 下載與安裝

下載這個專案（綠色 **Code** 按鈕 → **Download ZIP**），解壓縮。在資料夾裡開命令列：

```bash
pip install -r requirements.txt
```

### 2. 拿到 LINE 的兩把鑰匙

1. 到 [LINE Official Account Manager](https://manager.line.biz/) 建立官方帳號。
2. 到 [LINE Developers Console](https://developers.line.biz/console/) 建 Provider → 建 **Messaging API** channel，綁定剛剛的官方帳號。
3. 複製 **Channel secret**（Basic settings 分頁）和 **Channel access token (long-lived)**（Messaging API 分頁按 Issue）。

### 3. 填設定檔

把 `.env.example` 複製一份改名成 `.env`，用記事本打開：

```
LINE_CHANNEL_SECRET=剛剛複製的 channel secret
LINE_CHANNEL_ACCESS_TOKEN=剛剛複製的 access token
BOT_OWNER_CLAIM_CODE=自己想一組認證碼，例如 8 位數字
```

> `.env` 不會被上傳。**不要把 token 貼到聊天室或截圖給別人**，那等於整個官方帳號的控制權。

### 4. 改成你自己的問答

打開 `faq.json`：

| 欄位 | 說明 |
|---|---|
| `shop_name`、`welcome` | 店名、加好友時的歡迎詞 |
| `entries` | 每一題問答：`keywords` 是客人可能打的字，`answer` 是回覆 |
| `handoff_keywords` | 敏感詞，命中就一定轉真人（退款、客訴、過敏…） |
| `escalation_templates` | 「要老闆拍板」的情境（談價、月結…）和按鈕選項；`reply` 是 `null` 代表老闆自己打字 |
| `ai_notes` | （選用）只給 AI 看的補充說明 |

### 5. 啟動並讓 LINE 連得到

- **Windows**：雙擊 **`背景啟動.vbs`**（背景執行，不會跳黑色視窗；紀錄寫在 `data\bot.log`）。要停就雙擊 **`停止.vbs`**。
- **macOS／Linux**：`python3 app.py`

LINE 要能從網路連到你的電腦，最簡單是用 [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)：

```bash
cloudflared tunnel --url http://localhost:18800
```

把它印出的 `https://xxxx.trycloudflare.com` 加上 `/callback`，填到 LINE Developers Console → Messaging API → **Webhook URL**，按 **Verify**。同一頁打開 **Use webhook**，並在官方帳號後台把「自動回應訊息」關掉（不然會跟 bot 搶著回）。

> 臨時通道每次重開網址都會變，要重填一次。長期使用建議改用 cloudflared 的具名通道或自己的主機。

### 6. 綁定老闆

用老闆自己的 LINE 加官方帳號好友，傳：

```
我是老闆 你的認證碼
```

看到「認證完成 ✅」就好了。之後需要你決定的事都會推到這裡。可以綁定不只一位老闆／店長。

---

## 老闆在 LINE 裡能用的指令

| 打這個 | 會發生什麼 |
|---|---|
| `我是老闆 認證碼` | 綁定身分（只做一次） |
| `待辦` | 把還沒決定的事再推一次卡片 |
| `重載` | `faq.json` 改過後重新載入，不用重開 |
| `說明` | 顯示指令 |
| 直接打任何字 | 原話轉給最近一位在等回覆的客人（按過「我自己回」的優先） |

---

## 選用：讓 Claude 幫忙補答

FAQ 沒把握的問題，可以先讓 Claude 依 `faq.json` 的內容回答一次；Claude 判斷知識庫沒寫、要店家決定，或是客訴退款這類事情，就不答，照樣推給老闆。

1. 到 [Claude Console](https://console.anthropic.com/) 申請 API key。
2. 在 `.env` 填 `ANTHROPIC_API_KEY=...`（模型可用 `BOT_AI_MODEL` 改，預設 `claude-sonnet-5-5`）。
3. 重新啟動。`http://localhost:18800/health` 會顯示 `"ai": true`。

知識庫放在 system prompt 最後一段並開 prompt caching，重複的知識庫內容不會每次都算全價。會花 API 費用，請自己留意用量。

---

## 給開發者

| 檔案 | 用途 |
|---|---|
| `app.py` | Flask webhook：驗簽 → 丟佇列 → 立刻回 200；背景執行緒處理 |
| `bot.py` | 客服大腦：敏感詞 → FAQ → AI → 升級給老闆；老闆指令與按鈕 |
| `faq.py` | 關鍵字加權比對 |
| `llm.py` | 選用的 Claude 補答（prompt caching、拒答就交給人） |
| `line_api.py` | LINE API（只用標準庫）與決策卡片 |
| `db.py` | SQLite：聯絡人、訊息、待決事項 |

測試（不打 LINE、不打 Claude）：

```bash
python -m unittest discover -s tests
```

---

## 常見問題

| 症狀 | 原因與解法 |
|---|---|
| 啟動說「缺少必要設定」 | `.env` 沒建立或值沒填，回到步驟 3 |
| Webhook Verify 失敗 | bot 沒開，或 cloudflared 網址換了 |
| 客人傳訊息沒反應 | LINE Developers → Messaging API → **Use webhook** 沒打開；看 `data\bot.log` |
| bot 跟自動回應一起回 | 官方帳號後台 → 回應設定 → 關掉「自動回應訊息」 |
| 老闆收不到卡片 | 還沒傳「我是老闆 認證碼」，或認證碼打錯 |
| `dataot.log` 出現「LINE API 失敗 … 401」 | Channel access token 填錯或已重新 Issue，回到步驟 2 重新複製、更新 `.env` 後重啟 |

## 授權

[Apache License 2.0](LICENSE)

## 🛠️ 需要客製化？

這個工具是開源的，可以免費使用。如果你想要：
- 改成符合你們公司的流程、欄位或品牌
- 串接你現在在用的系統（LINE、Google 表單、POS、會計軟體……）
- 幫你安裝、架站、教學，或長期維護

歡迎加 LINE 跟我們聊。我們會依需求的範圍與複雜度報價，原則上以實際報價單為準。

<a href="https://lin.ee/pdYGARu"><img src="https://scdn.line-apps.com/n/line_add_friends/btn/zh-Hant.png" alt="加入好友" height="36"></a>

- 💬 LINE 官方帳號：[@812ibtir](https://lin.ee/pdYGARu)（請註明「LINE 客服機器人 客製化」）
- 🏢 留白事務所：網頁設計、AI 自動化、房仲遠端助理
- 👤 作者：[林其衞 Larry](https://github.com/Larryveryhandsome)
