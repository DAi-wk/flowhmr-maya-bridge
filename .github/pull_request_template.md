<!--
謝謝你的 PR！請先看過 CONTRIBUTING.md。中文或英文都可以。
Thanks for the PR! Please read CONTRIBUTING.md first. Chinese or English is fine.
-->

## 變更內容 / What changed



## 原因 / Why

相關 issue / Related issue：

## 測試 / Testing

- [ ] `python -I tests/test_automap.py -v`
- [ ] `python -I tests/test_mapping.py -v`
- [ ] `python -I tests/test_client.py -v`
- [ ] 在 Maya 裡實測，只用拋棄式場景 / Tested in Maya, in a disposable scene only
- [ ] 不影響套用結果，不需要在 Maya 實測 / Doesn't affect apply results, no Maya test needed

有在 Maya 實測的話 / If tested in Maya：

- Maya 版本 / version：
- FlowHMR 版本 / version：
- rig 類型和版本（ADV、Hive、mGear、其他）/ Rig type and version：
- namespace：有 / 沒有 · yes / no
- 模式 / Mode：原始動作 → ADV / ADV 動捕庫 / HMR 角色 FBX / 自訂對應
- 動作幀數 / Frame count：
- 量到的結果（旋轉、位置誤差）/ Measured result (rotation, position error)：

## 檢查清單 / Checklist

- [ ] 沒有本機路徑、個人姓名或 email、專案裡實際的角色或 rig 名稱 / No local paths, personal names or emails, or real character or rig names
- [ ] 沒有加入 rig 檔、場景、FBX、影片、動作資料或第三方檔案 / No rig files, scenes, FBX, videos, motion data or third-party files
- [ ] 套用失敗時場景會回到套用前（節點、約束、key、動畫層、原動畫、幀率）/ A failed apply restores the scene
- [ ] 不修改 FlowHMR 的檔案；`client.py`、`mapping.py` 沒有 import `maya` / FlowHMR untouched; no `maya` import in `client.py` or `mapping.py`
- [ ] 行為或使用方式有改變時，已更新 README / README updated if behaviour changed
