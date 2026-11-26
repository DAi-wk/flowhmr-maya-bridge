# 參與貢獻

謝謝你願意改進 FlowHMR Bridge for Maya。歡迎回報問題、修 bug、補文件，或讓它支援更多 rig。

> 這是非官方的 Maya 外掛，和 FlowHMR 的作者沒有關係。FlowHMR 本身的問題請回報給 FlowHMR。

**English** — Issues and pull requests in English are welcome. See [English](#english) at the end.

## 開始之前

- 小修正（錯字、明顯的 bug、文件）可以直接開 PR。
- 較大的變更請先開 issue 討論做法，例如新的套用模式、會改變重定向結果的修改、動畫層或清理流程的修改、支援新的 rig 系統。
- 回報 bug 時請附上：Maya 版本、FlowHMR 版本、rig 類型（ADV 版本、Hive、mGear 或其他）、有沒有 namespace、使用的模式，以及 Script Editor 裡完整的錯誤訊息。請不要附 rig 檔、場景檔或影片。

## 開發環境

1. Fork 這個 repo，clone 到本機。
2. 在 Maya 的 Script Editor（Python 頁）載入你的工作副本：

   ```python
   import sys
   sys.path.insert(0, r"<你的 clone 路徑>")
   import flowhmr_bridge.ui
   flowhmr_bridge.ui.show()
   ```

   改完程式後，最保險的做法是重開 Maya 再載入一次。
3. 測試連線和套用需要 FlowHMR 桌面版和 Maya 在同一台電腦上執行。只改 `mapping.py` 或 `client.py` 的話，跑下面的測試就夠了。

## 測試

這些測試不需要 Maya，Python 3.7 以上都可以跑，也可以用 FlowHMR 附的 `runtime/python.exe`：

```
python -I tests/test_automap.py -v
python -I tests/test_mapping.py -v
python -I tests/test_client.py -v
```

`test_client.py` 在 FlowHMR 沒開時會略過連線的部分。

會改變套用結果的修改，還要在 Maya 裡實測：

- 只在**拋棄式的場景**或另開的 Maya 裡測，不要在有未存檔工作的場景裡測。`phase0_adv_test.py`、`stage1_generic_test.py`、`stage_generic_test.py`、`stage_raw_test.py` 都會對 rig 套用和烘焙動畫。
- 在 PR 裡寫下實測環境（Maya 版本、rig 類型和版本、有沒有 namespace、動作幀數），以及量到的結果，例如旋轉和位置誤差。
- README 裡的數字都是實測值。沒測過的情況請直接寫「沒測過」，不要用推測的數字。

## 設計原則

- **不修改 FlowHMR。** Bridge 只透過 FlowHMR 桌面版的本機 API 讀資料。`client.py` 只連 127.0.0.1，不改動 FlowHMR 的任務或設定；唯一的 POST 只是取得讀取用的 ticket。原始動作模式會用 Node.js 執行安裝版的 FlowHMR 預覽程式，但不改它的檔案。
- **不依賴 Maya 的模組維持不依賴。** `client.py` 和 `mapping.py` 不能 import `maya`，測試才能在 Maya 外面跑。
- **失敗時要清乾淨。** 套用途中任何一步失敗，都要回到套用前的狀態：刪掉匯入的節點、約束、這次新增的 key 和動畫層，接回原本的動畫，還原幀率。新增的套用步驟也要納入清理。
- **結果放在新的動畫層。** 不直接覆蓋使用者的動畫。把新的動畫層靜音，就要回到套用前的結果。
- **不要在執行中的 Maya 切換 evaluation manager 的模式**（例如 `cmds.evaluationManager(mode='off')`），實測會讓 Maya 當掉。
- **支援新的 rig 命名**：自動偵測的規則在 `mapping.py`，請在 `tests/test_automap.py` 加上這種命名的測試。

## 隱私與檔案

這是公開的 repo，commit 和 PR 的內容都會公開。

- 不要放本機路徑（例如 `C:\Users\...` 或你的專案資料夾）、個人姓名或 email。
- 不要用你專案裡實際的角色或 rig 名稱，請寫成「ADV 測試 rig」「Hive 測試 rig」這類泛稱。對應設定檔、測試資料和 log 範例裡的名稱也一樣。
- 不要提交 rig 檔、Maya 場景、FBX、影片、動作資料或量測報告。`.gitignore` 已經排除 `*.fbx`、`*_report*.json` 和 `staging/`。
- 不要提交 FlowHMR、AdvancedSkeleton、Zoo Tools、mGear 的任何檔案或程式碼片段，它們各有自己的授權。
- commit 的 email 建議用 GitHub 的 noreply 地址（GitHub 的 Settings → Emails → Keep my email addresses private）。

## 文件、commit 和 PR

- 使用方式或行為有改變時，請一起更新 `README.md`。README 以繁體中文為主，開頭附英文摘要；不熟中文的話用英文寫也可以。
- commit 訊息用英文：第一行簡短說明做了什麼，空一行後再條列細節。
- 一個 PR 只做一件事。開 PR 時會自動帶出範本，請照著填。
- PR 說明和討論用繁體中文或英文都可以。

## 授權

你提交的貢獻會以這個 repo 的 [MIT 授權](LICENSE) 釋出。

## English

Thanks for helping. The README is in Traditional Chinese with an English summary at the top; issues and pull requests in English are welcome.

- **Before you start**: open a PR directly for small fixes. For larger changes (a new apply mode, changes to retarget results, animation-layer or cleanup logic, a new rig system), open an issue first. Bug reports should include the Maya and FlowHMR versions, the rig type and version, whether it uses a namespace, the mode, and the full Script Editor error. Please don't attach rig files, scenes or videos.
- **Tests** run without Maya on Python 3.7+: `python -I tests/test_automap.py -v`, `python -I tests/test_mapping.py -v`, `python -I tests/test_client.py -v`. Changes that affect apply results also need a test in Maya, in a disposable scene only, and the PR should list the environment (Maya, rig type and version, namespace, frame count) and the measured error. Say "not tested" rather than estimating.
- **Design rules**: never modify FlowHMR's files; `client.py` talks only to 127.0.0.1 and does not change FlowHMR's jobs or settings; `client.py` and `mapping.py` must not import `maya`; a failed apply must restore the scene (nodes, constraints, keys, layers, original animation, frame rate); results go on a new animation layer; don't switch the evaluation manager mode in a running Maya (it crashes Maya).
- **Privacy**: this repo is public. No local paths, personal names or emails, and no real character or rig names from your projects (say "ADV test rig"). No rig files, scenes, FBX, videos, motion data, reports, or files from FlowHMR, AdvancedSkeleton, Zoo Tools or mGear. A GitHub noreply commit email is recommended.
- **Commits**: English, a short summary line, a blank line, then bullet details. One change per PR; update the README when behaviour changes.
- Contributions are released under the MIT License.
