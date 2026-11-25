# FlowHMR Bridge for Maya

把 FlowHMR 生成好的動作，直接套用到 Maya 裡的 rig，不用手動匯出、匯入 FBX。AdvancedSkeleton rig 走 ADV 自己的動捕庫；其他 rig 用「自訂對應」指定每個關節由哪個控制器負責。

> 這是非官方的 Maya 外掛，和 FlowHMR 的作者沒有關係。它不修改 FlowHMR 的任何檔案，只從 FlowHMR 桌面版的本機網頁 API（127.0.0.1）讀取任務和下載 FBX。這組 API 是 FlowHMR 介面自己用的，沒有公開文件，FlowHMR 更新後可能會變動。

**English:** An unofficial Maya add-on that pulls finished motion from the FlowHMR desktop app's local web API (loopback only, read-only) and applies it to a rig: AdvancedSkeleton rigs through ADV's MoCapLibrary, any other rig through a HumanIK/Humanoid-style mapping editor with FK, IK and pole-vector support. It does not modify FlowHMR. The API it uses is FlowHMR's internal web-UI API and may change between FlowHMR releases. Documentation below is in Traditional Chinese.

## 需求

- FlowHMR 桌面版，和 Maya 在同一台電腦上執行（測試版本 v0.3.9）。
- Maya 2025（Python 3）。Maya 2024 應該也能用，但沒有完整測過。
- ADV 模式需要 AdvancedSkeleton（測試版本 6.755）。自訂模式不需要任何外掛。

面板會讀 FlowHMR 資料夾裡的 `.cache/desktop-port.txt` 找連線埠，找不到時用預設的 60550。FlowHMR 資料夾的位置可以在面板上填，或設定環境變數 `FLOWHMR_ROOT`；兩者都沒有時，會找這個 repo 旁邊名為 `FlowHMR` 的資料夾。

## 安裝

把 `install_flowhmr_bridge.py` 拖進 Maya 視窗，它會在目前的 shelf 加一個 **FlowHMR** 按鈕，並開啟面板。要移除時，刪掉這個按鈕即可。

如果不想加按鈕，可以在 Script Editor 的 Python 頁執行：

```python
import sys
sys.path.insert(0, r"<這個 repo 的路徑>")
import flowhmr_bridge.ui
flowhmr_bridge.ui.show()
```

## 使用

1. 啟動 FlowHMR 桌面版，在那邊匯入影片、選人、生成。面板會自動連線，每 3 秒更新一次任務列表。
2. 在 Maya 開一個**控制器沒有動畫**的場景，把 rig 參照或匯入進來。
3. 在面板中選一個「完成」的任務，再選 **rig 類型**：
   - **AdvancedSkeleton**：選目標 rig，按 **套用到 ADV rig**。
   - **自訂對應**：選一份對應設定，面板會自動找出場景裡符合的 namespace，按 **套用到自訂 rig**。還沒有設定時，按 **新增…** 開啟自訂對應面板。
4. 選完成後的幀率。

ADV 模式的套用流程是：
- 先把場景暫時切到 30 FPS，用 ADV MoCapLibrary 接上約束，再用 Bake Body 烘焙。
- 接著跑 euler filter，最後換到你選的幀率。key 以秒數保留時間，所以 30、60、120 fps 會落在整數幀。
- 第一幀（30 FPS 時的第 −1 幀）是 T Pose 參考。

任何一步失敗，都會清掉匯入的動捕骨架、約束，以及這次新增的 key，並還原幀率。rig 的姿勢（build pose）和 FKIKBlend 會維持 ADV 設定的值：腿 IK、手臂 FK。

**套用後不能用 Ctrl+Z 復原。** ADV 匯入 FBX 的步驟不能 undo，還會清空它之前的 undo 紀錄。實測只按一次 Ctrl+Z，會把刪掉的動捕骨架帶回來，場景也會卡在 30 FPS。所以套用結束後會主動清空 undo 紀錄。要重來的話，請刪除 rig 上的 key，或重新開檔。

## 自訂對應：人形定義（任意 rig）

在主面板按 **新增…** 或 **編輯…**，會開啟獨立的「FlowHMR 人形定義」面板，操作方式參考 Maya HumanIK 的 Character Definition 和 Unity 的 Humanoid Avatar：

1. **namespace**：選 rig 所在的 namespace。存下來的控制器名稱不含 namespace，同一份定義可以用在不同 namespace 的同一個 rig 上。
2. **自動偵測**：依控制器名稱自動配對，可辨識 ADV、Zoo Hive、mGear 這類命名，也會略過 IK、pole、twist、space、sub 等輔助控制器，脊椎則依高度排序分配。偵測後請逐一檢查。
3. **分頁**：
   - **身體**：正面視角的人形圖，角色的左邊在畫面右邊。
   - **右手**、**左手**：手指圖。
   - **清單**：原本的表格。
4. **圓點**：
   - 實線圈＝必要部位：Root、頭、脊椎至少一節、上臂、前臂、手腕、大腿、小腿、腳踝。虛線圈＝可選。
   - 綠色＝可用；橘色＝控制器已被 rig 內部驅動或鎖住，套用時會略過；紅色＝找不到，或必要部位還沒指定；菱形是 Root／COG。
5. **指定控制器**：
   - 點圓點選取部位；**雙擊**＝把 Maya 目前選取的控制器指定給它；**右鍵**有選單。
   - 在 Maya 依序選多個控制器再按 **指定選取的控制器**，會從目前的部位開始沿著順序連續填入。例如從鎖骨開始，依序選鎖骨、上臂、前臂、手腕。
   - 勾 **指定後跳到下一個部位** 可以連續作業；可選的「手掌方向參考」會被跳過。
   - 也可以在下方「控制器」欄位直接輸入名稱。
6. **左 → 右鏡像**：依左側的名稱猜右側控制器（`_L`→`_R`、`Left`→`Right`、`l_`→`r_` 等），只填空白的部位，而且只填場景裡真的存在的控制器。
7. **靜置姿勢**：
   - 「控制器歸零」：套用前把對應到的控制器歸零，大多數 rig 歸零就是綁定姿勢。
   - 「目前姿勢」：用 rig 目前的姿勢當靜置姿勢。T Pose 或 A Pose 都可以，引擎會把 FlowHMR 的骨頭方向對齊到 rig。
8. **烘焙時固定的屬性**：例如 FK/IK 切換。可以在 Maya 選控制器、在 Channel Box 選屬性，再按 **加入 Channel Box 選取的屬性**。
9. **儲存**：存到 `Documents/maya/FlowHMRBridge/profiles/`，主面板會自動列出。必要部位沒指定完時，存檔前會提醒。也可以 **從範本** 載入內建設定再修改；內建範本不能直接覆寫，會另存一份。

### IK 和 Pole

身體圖上的 ■ 是 IK 控制器，▲ 是 Pole，左右各有五個可選部位：IK 手腕控制器、手肘 Pole、IK 腳控制器、膝蓋 Pole、IK 腳尖。自動偵測認得 ADV（`IKArm/PoleArm/IKLeg/PoleLeg/IKToes`）、Hive（`*_wrist_ik_anim`、`*_elbowPv_pv_anim`、`*_foot_ik_anim`、`*_kneePv_pv_anim`、`*_ball_ik_anim`）和 mGear（`*_ik_ctl`、`*_upv_ctl`）的命名。

套用時：
- **IK 控制器的位置**＝rig 自己的上臂（或大腿）位置 ＋ 來源「上臂→手腕」（或「大腿→腳踝」）的向量 × 這條肢體的長度比。用即時的節點網路計算，所以 rig 的肩膀或髖怎麼動，手腳都會停在正確的相對距離，原本踩穩的腳也比較不會滑。旋轉跟著來源的手腕或腳踝。
  - 要用這個做法，必須同時指定 FK 的上臂或大腿；沒有的話，會改用固定偏移。
- **Pole**：逐幀依來源手肘或膝蓋彎曲的方向擺放。四肢伸直時，改用 rig 靜置時的 pole 方向，跟著上臂或大腿一起轉，兩種方向平滑混合，伸直時不會亂跳或翻轉。Pole 的位置同樣換算到 rig 的空間，所以彎曲平面會和來源一致。
- **IK 腳尖**：跟腳尖 FK 一樣用 orient 約束。

**四肢 FK／IK 表**：
- 每條手臂、腿選「顯示 FK」或「顯示 IK」，填上切換屬性和 FK、IK 各自的值。
- 按 **偵測切換屬性**，會在四肢控制器和名稱含 fkik／ikfk／switch／ui／settings 的控制器上，找像 `FKIKBlend`、`ikfk`、`*_blend` 的屬性，並依名稱和數值範圍猜 FK、IK 值，請在 Maya 確認一次。偵測到的屬性會從「其他固定屬性」移除，避免重複。
- 有對應到的 FK 和 IK 都會烘焙，之後可以自由切換。這張表只決定套用後顯示哪一個。

實測（example.mp4）：

| rig | 伸展距離誤差 | 彎曲平面夾角（平均） | 手腕、腳踝旋轉 |
| --- | --- | --- | --- |
| ADV 測試 rig（手腳都用 IK） | 0.1–0.3 cm | 0.07–0.7° | 0° |
| Zoo Hive 測試 rig（手腳都用 IK） | 0.05–0.5 cm | 0.07–0.6° | 腳 1–2°；手腕見下方說明 |

Hive 測試 rig 的手腕在 IK 模式下，是透過額外加上的一層控制器接到手腕骨頭，而這一層對 Hive IK 手腕控制器的 orientConstraint 權重是 **0**，所以 IK 手腕控制器的旋轉傳不到手腕，平均差 30°。把那個約束的權重改成 1 測試，手腕就完全跟上（0°）。如果你的 rig 在 IK 控制器上也有類似的額外層，請先檢查這類約束。

自訂模式目前的限制：
- 手指 IK（自訂的手指 IK 系統）還不支援，手指只能用 FK。
- rig 不能已經有動畫，覆蓋功能目前只有 ADV 模式有。
- 骨盆不做方向對齊，所以骨盆到胸口會有幾度的固定差。

## 覆蓋現有動畫（ADV 模式）

rig 已經有 key 時，勾選 **覆蓋現有動畫** 再按套用，面板會先跳出確認視窗。確認後：

1. 原本的動畫曲線會從控制器拆下來，備份到場景裡的 `FlowHMR_animBackup_<日期時間>` 節點。沒有 key 的控制器數值和播放範圍也一起記下來。
2. rig 回到 build pose，照一般流程套用 FlowHMR 的動作。
3. 想找回原動畫，按 **還原覆蓋前的動畫**。它會刪掉 FlowHMR 的 key、接回備份的曲線、還原數值和播放範圍，最後刪除備份節點。如果有多個備份，會還原最新的那個。

- 套用途中失敗時，會自動還原原動畫。
- 備份存在場景裡，**存檔後才會留下來**。
- 不需要的備份可以直接刪掉 `FlowHMR_animBackup_…` 節點。原曲線會留在場景中，但沒有連到任何東西，可以用 Optimize Scene Size 清掉。
- 如果控制器的動畫在動畫層或 pairBlend 上，沒辦法自動備份，面板會拒絕套用。請先合併動畫層。

## 限制（v0.1）

- 只支援單人任務。多人場景任務會列出，但不能選。
- rig 已經有動畫時，必須勾選「覆蓋現有動畫」才能套用。ADV 用第 −1 幀的姿勢計算偏移，而且會重烘整個 ControlSet，所以原動畫要先拆下來備份。
- 實測環境：AdvancedSkeleton 6.755、Maya 2025；rig 用一層 namespace 參照，或沒有 namespace 的參照，兩種都測過。其他 ADV 版本會顯示提醒。對沒有 namespace、而且已經有動畫的 rig 做「覆蓋」還沒實測。
- 針對 ADV 6.755、有 namespace 的 rig，修正了兩個問題：
  - 套用時 namespace 會遺失，改由 `$gAsPanelNamespace` 帶入。
  - 手指控制器會被加上兩次 namespace，改由面板自己補上手指約束。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `flowhmr_bridge/client.py` | 和 FlowHMR 本機 API 通訊，只讀取、只連 127.0.0.1，不依賴 Maya |
| `flowhmr_bridge/timing.py` | 30 FPS 匯入、烘焙，以及完成後換幀率 |
| `flowhmr_bridge/adv.py` | ADV MoCapLibrary 套用、烘焙、清理 |
| `flowhmr_bridge/ui.py` | Maya 主面板（ADV／自訂模式切換） |
| `flowhmr_bridge/mapping_ui.py` | 人形定義面板（身體圖、手指圖、清單） |
| `flowhmr_bridge/mapping.py` | 人形定義的部位欄位、必要部位、讀寫、鏡像、自動偵測規則，不依賴 Maya |
| `tests/test_automap.py` | 自動偵測測試（ADV、Hive、mGear 命名）：`python -I tests/test_automap.py -v` |
| `tests/test_mapping.py` | 對應設定讀寫測試：`python -I tests/test_mapping.py -v` |
| `phase0_adv_test.py` | 呼叫同一個 `adv.apply()`，並量測重定向誤差和烘焙漂移 |
| `flowhmr_bridge/generic.py` | 通用引擎：依對應設定把 FlowHMR 的 `flowhmr` FBX 套到任意 rig 的 FK 控制器，含靜置姿勢校正 |
| `flowhmr_bridge/profiles/*.json` | 對應設定檔。`adv_generic_test.json` 是拿 ADV rig 測試通用引擎用的 |
| `stage1_generic_test.py` | 第 1 階段測試：通用引擎和 ADV 結果對照、手臂垂下的靜置姿勢、失敗清理 |
| `stage_generic_test.py` | 不限 rig 的測試腳本，量測用的骨頭對應寫在設定檔的 `test` 區塊 |
| `flowhmr_bridge/profiles/zoo_hive_biped.json` | Zoo Hive 的身體和四肢 FK 對應。手指不在裡面，因為測試用的 Hive rig 手指由自訂的 IK 系統驅動 |
| `tests/test_client.py` | client 測試，FlowHMR 沒開時會略過連線部分：`python -I tests/test_client.py -v` |

測試不需要 Maya，任何 Python 3.7 以上都可以跑，包括 FlowHMR 附的 `runtime/python.exe`。`phase0_adv_test.py`、`stage*_generic_test.py` 要在 Maya 裡、拋棄式的場景中執行，會對 rig 套用和烘焙動畫。

## 授權

MIT，見 [LICENSE](LICENSE)。FlowHMR、AdvancedSkeleton、Zoo Tools、mGear 各自依它們的授權條款，這個 repo 不包含它們的任何檔案。
