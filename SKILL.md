---
name: youtube-english-study
description: Turn a YouTube video into an English study session — get the transcript, write a learner-level English article from it, pick the key vocabulary, and run practice exercises. Use this skill whenever the user gives a YouTube link (youtube.com / youtu.be) and wants to learn English from it, or says things like "用這個影片學英文", "整理單字", "幫我出練習題", "study English with this video", even if they don't say "skill" or list every step.
---

# YouTube English Study

把一支 YouTube 影片變成一次完整的英文學習：**取得逐字稿 → 整理文章 → 重點單字 → 練習**。

## 預設設定

- 說明文字一律用繁體中文；英文內容（文章、例句、單字）保持英文。
- 學習者程度：中級（約 CEFR B1–B2）。單字挑選、文章難度、練習題都以此為準；使用者另外指定程度時以使用者為準。
- 學習者習慣用 shadowing（跟讀）練習，練習環節要包含跟讀句。

## Step 1：取得逐字稿

1. 先用 `web_fetch` 抓使用者給的 YouTube 網址，看能否取得 transcript / captions。
2. 抓不到（常見：YouTube 頁面不含字幕、需要登入、網路被擋）時，**不要猜內容、不要只靠標題或描述編造**。直接請使用者貼逐字稿，並附上取得方法：
   > 在 YouTube 影片下方描述欄點「...更多」→「顯示轉錄稿」，把全文複製貼上即可（時間戳記可保留）。
3. 若環境有 bash 且可連 YouTube（例如 Claude Code），可改用 `youtube-transcript-api` 或 `yt-dlp --write-auto-subs --skip-download` 取得字幕。
4. 影片不是英文、或字幕品質太差（大量錯字、無標點）時，先告知使用者再繼續。

## Step 2：整理英文文章

- 依逐字稿內容，用自己的話寫一篇 **200–350 字**的英文文章（graded reading），難度 B1–B2，保留影片的重點與脈絡。
- 不要整段照抄逐字稿；需要引用原句時，只引用短句並標註時間戳記（例如 `[03:12]`）。
- 文章後附 **3–5 句中文重點摘要**。

## Step 3：重點單字（10–15 個）

挑選原則：
- 優先挑 B1–C1 範圍、影片中實際出現、且實用的字；跳過太基礎的字（如 make, good）。
- 包含 phrasal verbs、collocations、慣用語（例如 *come up with*, *make a point of*）。
- 同一字根只列一次。

每個單字用表格呈現：

| Word / Phrase | 詞性 | KK 音標 | 中文意思 | 影片原句（含時間戳記） | 補充例句 |
|---|---|---|---|---|---|

表格後列出 **3–5 句跟讀句（shadowing sentences）**：從影片挑語調自然、含重點單字的短句，附時間戳記，方便使用者回到影片跟讀。

## Step 4：練習

整理完後詢問使用者要不要開始練習。練習時：

1. **選擇題（字義）**：5 題。若有 `quiz_display_v0` 工具就用它出題，否則用文字一次一題。
2. **克漏字**：用文章或影片句子挖空 5 題，空格只放本次的重點單字。
3. **中翻英**：3 題，句子要能用上重點單字。使用者作答後，指出文法或用字問題並給較自然的說法。
4. **跟讀提醒**：請使用者回到 Step 3 的時間戳記跟讀，可請他打出聽到的句子做 dictation 對照。

批改原則：
- 一次一題，等使用者回答後再出下一題。
- 答錯時說明原因（意思混淆、詞性、搭配詞），不要只給答案。
- 練習結束後統計答對率，列出答錯的單字建議複習。

## Step 5：輸出網頁練習檔（Shadowing Studio）

若環境可寫檔（例如 Claude Code，且專案內有 `index.html` / `server.py`），整理完 Step 1–3 後，**同時**把內容存成 `lessons/<videoId>.json`，供本機網頁載入練習。只有對話、無法寫檔時略過此步。

### 兩種產生方式

網頁載入沒有練習檔的影片時，會讓使用者二選一：

| | 方法一：用 Claude API key 自動產生 | 方法二：複製指令，由 Claude 手動產生 |
|---|---|---|
| 怎麼做 | 在網頁按「自動產生題目」 | 複製網頁上的指令，貼到 Claude Code |
| 誰產生 | `server.py` 呼叫 Claude API（`claude-opus-5-5`，結構化輸出） | Claude Code 依本 SKILL 產生 |
| 需要 | Anthropic API key（`api_key.txt` 或環境變數 `ANTHROPIC_API_KEY`），依用量計費，一支 30–40 分鐘影片約 US$0.25 | 可用的 Claude Code，不需要 API key |
| 耗時 | 約 1–3 分鐘，完成後網頁自動顯示 | 視對話而定，完成後在網頁按「重新載入影片」 |

兩種方式都輸出同一份 `lessons/<videoId>.json`（格式見下方），同一支影片只需產生一次。

**收到方法二的指令時**（例如「幫我用 youtube-english-study 產生 <網址> 的練習，存成 lessons/<id>.json」）：
- 依 Step 1–3 的規則產生內容，並依 Step 5 存檔；
- 對話中只需簡短回報（影片標題、單字清單、跟讀句、檢查結果），不必貼出完整文章與表格，也不必進入 Step 4 的互動練習，因為練習在網頁上進行；
- 存檔後提醒使用者在網頁按「重新載入影片」。

**維護注意**：方法一的出題規則寫在 `server.py` 的 `SYSTEM_PROMPT` 與 `LESSON_SCHEMA`。修改本 SKILL 的出題規則或 JSON 格式時，要同步修改這兩處，兩種方式產出的內容才會一致。

- 字幕可用 `server.py` 的快取 `transcripts/<videoId>.json`，或 `youtube-transcript-api` 取得。
- 時間一律用**秒數**（數字），從逐字稿的 `start` 取值，不要估。
- `shadowing` 的 `end` 取下一行字幕的 `start`，讓網頁能剛好播完該句。
- `turns`（選用，使用者要求精確標註說話者時才做）：每次換人說話時記一筆，`start` 取該句逐字稿的秒數。有 `turns` 時網頁以它為準；沒有時網頁自動從字幕判斷說話者名字（判斷不出則用 Speaker A / B）。
- 題目內容與 Step 4 規則相同：選擇題 5、克漏字 5（空格用 `______`，答案只放重點單字）、中翻英 3。

```json
{
  "videoId": "<11 碼 id>",
  "title": "<影片標題>",
  "level": "B1–B2",
  "turns": [{"start": 0, "speaker": "<說話者名字>"}],
  "article": "<英文文章，段落間用 \\n\\n 分隔>",
  "summary_zh": ["<中文重點 3–5 句>"],
  "vocab": [
    {"word": "", "pos": "", "kk": "[...]", "zh": "", "quote": "<影片原句>", "time": 0, "example": ""}
  ],
  "shadowing": [
    {"text": "<影片原句>", "start": 0, "end": 0}
  ],
  "mcq": [
    {"word": "<考的單字>", "q": "", "options": ["", "", "", ""], "answer": 0, "explain": "<答錯原因說明>"}
  ],
  "cloze": [
    {"sentence": "... ______ ...", "answer": "", "hint": "<首字母…（中文）>", "explain": ""}
  ],
  "translate": [
    {"zh": "", "reference": "<參考英譯>", "keywords": ["<應用到的重點單字>"]}
  ]
}
```

存檔後告訴使用者：在網頁按「重新載入影片」即可練習（`start.bat` 或 `python server.py` → http://localhost:8000）。若使用者是用 `release\ShadowingStudio` 的 exe 版，練習檔要放進該資料夾的 `lessons\`。

## 輸出格式總覽

```
## 📺 影片：<影片標題>
## 📖 Article
<英文文章>
### 中文重點摘要
<3–5 句>
## 📝 Key Vocabulary
<單字表格>
### 🎙️ Shadowing Sentences
<3–5 句 + 時間戳記>
## ✏️ 要開始練習嗎？
```
