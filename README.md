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
2. 在 Maya 開場景，把 rig 參照或匯入進來。rig 已經有動畫也可以，見下方「動畫層與覆蓋」。
3. 在面板中選一個「完成」的任務，再選 **rig 類型**：
   - **AdvancedSkeleton**：選目標 rig，按 **套用到 ADV rig**。
   - **自訂對應**：選一份對應設定，面板會自動找出場景裡符合的 namespace，按 **套用到自訂 rig**。還沒有設定時，按 **新增…** 開啟自訂對應面板。
4. 選完成後的幀率，按套用。FlowHMR 的動作會放在一個新的動畫層上。

ADV 模式的套用流程是：
- 先把場景暫時切到 30 FPS，用 ADV MoCapLibrary 接上約束，再用 Bake Body 烘焙。
- 接著跑 euler filter，最後換到你選的幀率。key 以秒數保留時間，所以 30、60、120 fps 會落在整數幀。
- 第一幀（30 FPS 時的第 −1 幀）是 T Pose 參考。

任何一步失敗，都會清掉匯入的動捕骨架、約束、這次新增的 key 和動畫層，接回原本的動畫，並還原幀率。動畫層裡的 FKIKBlend 是 ADV 設定的值：腿 IK、手臂 FK。

**套用後不能用 Ctrl+Z 復原。** ADV 匯入 FBX 的步驟不能 undo，還會清空它之前的 undo 紀錄。實測只按一次 Ctrl+Z，會把刪掉的動捕骨架帶回來，場景也會卡在 30 FPS。所以套用結束後會主動清空 undo 紀錄。要撤銷這次套用，請刪除它的動畫層（或按「移除最新的 FlowHMR 動畫層」）。

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
   - 「目前姿勢」：用 rig 目前的姿勢當靜置姿勢。T Pose 或 A Pose 都可以。
   - 兩種都一樣：上臂、前臂、手掌、大腿、小腿和手指會把 FlowHMR 的骨頭方向對齊到 rig，所以 T Pose 和 A Pose 的結果相同。鎖骨、脊椎、頸、頭、腳掌和腳尖不對齊方向，而是「靜置對靜置」，只套用動作的旋轉變化，和 ADV MoCapLibrary 的做法一樣。這些部位在兩邊的靜置姿勢本來就相同（站直、腳掌平放），方向不同只是關節位置不同；硬對齊會變成聳肩、腳掌翹起、脖子前傾。
   - 用「目前姿勢」時，FK 和 IK 控制器要擺在同一個姿勢；只轉 FK 手臂、IK 手腕還在原位，IK 手會整段偏掉。
8. **烘焙時固定的屬性**：例如 FK/IK 切換。可以在 Maya 選控制器、在 Channel Box 選屬性，再按 **加入 Channel Box 選取的屬性**。
9. **儲存**：存到 `Documents/maya/FlowHMRBridge/profiles/`，主面板會自動列出。必要部位沒指定完時，存檔前會提醒。
   - **preset 不能直接修改**：內建範本，以及用 **匯入 preset…** 匯入的設定檔（複製到 `Documents/maya/FlowHMRBridge/presets/`），都只能讀。一修改，面板上方就會出現提示和「另存…」按鈕；按「儲存」也會改成另存新檔，名稱自動加上「（複本）」。preset 資料夾不能存檔。想更新 preset，就另存後再匯入一次。
   - **還沒儲存的修改不會默默消失**：開新、開啟其他檔案、從範本載入，或從主面板開啟另一個設定時，會先問要不要儲存（可以取消）。直接關掉視窗時也會問；如果沒有存也沒有選「不儲存」，會自動在 profiles 資料夾留一份 `<名稱>_未儲存_<時間>.json`。
   - 也可以 **從範本** 載入內建範本或 preset，當作新設定的起點。

### IK 和 Pole

身體圖上的 ■ 是 IK 控制器，▲ 是 Pole，左右各有五個可選部位：IK 手腕控制器、手肘 Pole、IK 腳控制器、膝蓋 Pole、IK 腳尖。自動偵測認得 ADV（`IKArm/PoleArm/IKLeg/PoleLeg/IKToes`）、Hive（`*_wrist_ik_anim`、`*_elbowPv_pv_anim`、`*_foot_ik_anim`、`*_kneePv_pv_anim`、`*_ball_ik_anim`）和 mGear（`*_ik_ctl`、`*_upv_ctl`）的命名。

套用時：
- **IK 手腕的位置**＝FK 手臂算出來的手腕位置。只要 FK 的上臂、前臂、手腕都有指定，IK 手臂就會和 FK 手臂彎得一模一樣，rig 的上臂、前臂比例跟來源不同也一樣，套用後在 FK／IK 之間切換不會跳。
- **IK 腳的位置**＝rig 自己的大腿位置 ＋ 來源「大腿→腳踝」的向量 × 腿長比。用即時的節點網路計算，所以髖怎麼動，腳都會停在正確的相對距離，原本踩穩的腳比較不會滑。手臂沒有完整指定 FK 時，IK 手腕也用這個做法。
  - 要用這個做法，必須同時指定 FK 的上臂或大腿；沒有的話，會改用固定偏移。
- IK 手腕和 IK 腳的旋轉跟著來源的手腕或腳踝。
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

這張表是在「鎖骨、脊椎、頭頸、腳掌改成靜置對靜置」之前量的。調整後在 ADV 測試 rig 上重測：和 ADV 原生結果相比，脊椎、頸、頭、鎖骨、腳掌、腳尖都是 0°，手臂顯示 FK 時上臂、前臂也是 0°；剩下手掌方向固定差 5–7°（自訂模式會對齊手掌方向），以及腿 2–3°（ADV 不縮放動捕，自訂模式依腿長縮放）。IK 手腕改成跟著 FK 之後，在 Hive 測試 rig 上重測：IK 和 FK 手臂的手腕位置一致、手肘差不到 0.4 cm，上臂、前臂方向和來源差 0.03° 以內；脊椎、頸、頭、鎖骨 0°，腳掌 1–2°。手腕旋轉仍受下方說明的 rig 約束影響。

Hive 測試 rig 的手腕在 IK 模式下，是透過額外加上的一層控制器接到手腕骨頭，而這一層對 Hive IK 手腕控制器的 orientConstraint 權重是 **0**，所以 IK 手腕控制器的旋轉傳不到手腕，平均差 30°。把那個約束的權重改成 1 測試，手腕就完全跟上（0°）。如果你的 rig 在 IK 控制器上也有類似的額外層，請先檢查這類約束。

自訂模式目前的限制：
- 手指 IK（自訂的手指 IK 系統）還不支援，手指只能用 FK。
- IK 腳用「伸展 × 腿長比」放置，所以 rig 的大腿、小腿比例跟來源差很多時，IK 腿的膝蓋彎曲會和 FK 腿差幾度（測試 rig 上 1–3°）。

## 動畫層與覆蓋（兩種模式都一樣）

每次套用都會建立一個新的**覆蓋（override）動畫層**，名稱是 `FlowHMR_<namespace>_<影片名>`，FlowHMR 的動作全部在這一層上：

- 把這一層**靜音**或權重調成 0，就看到原本的動畫；調成 0–1 之間可以混合。
- 再套用一次會疊一個新的動畫層在上面，舊的動畫層不會被改到。
- 動畫層裡除了烘焙的曲線，也會記錄這次套用改變的值，例如 FK/IK 切換、靜置姿勢、ADV 的 build pose。所以只開這一層就是完整的結果，靜音後也會回到原本的設定。少數屬性（例如 proxy 切換屬性）不能放進動畫層，會直接設定，log 會列出來。

rig 已經有動畫時，要勾選 **rig 已有動畫時也套用**。套用時會：

1. 把要烘焙的通道上的動畫（key、其他動畫層、pairBlend）暫時拆下。拆下期間，場景裡的 `FlowHMR_detached_…` 節點會記錄原本的連線。
2. 照一般流程接上約束、烘焙。
3. 把烘焙結果移到新的動畫層，原本的動畫接回動畫層下方，沒有 key 的控制器數值也設回去。

- 套用途中失敗時，會自動接回原動畫。萬一連清理都失敗，留下的 `FlowHMR_detached_…` 節點可以用面板右邊的 **接回上次拆下的動畫** 按鈕接回。
- **移除最新的 FlowHMR 動畫層**：刪掉這個 rig 最新的 FlowHMR 動畫層，回到下層的動畫和套用前的播放範圍。幀率不會改回。
- 場景裡如果還有舊版的 `FlowHMR_animBackup_…` 備份節點，同一個按鈕會改成 **還原舊版的覆蓋備份**。
- 動畫層存在場景裡，存檔後才會留下來。
- rig 檔案裡參照進來的空動畫層（例如 `BaseAnimation`）不影響使用，FlowHMR 的動畫層會放在場景的根動畫層下面。

實測：在已有動畫的 rig 上，動畫層打開時和烘焙結果一致（0 cm），靜音或刪除後和套用前一致（0 cm），疊兩層再逐層刪除也一致。60 fps 場景裡原本 key 的時間不變。強制在中途失敗時，原動畫完整接回。ADV 模式和自訂模式，在有 namespace 的 ADV、Hive 測試 rig，以及沒有 namespace、帶參照動畫層的 ADV 測試 rig 上都測過。

## 限制（v0.1）

- 只支援單人任務。多人場景任務會列出，但不能選。
- rig 已經有動畫時，必須勾選「rig 已有動畫時也套用」。ADV 會重烘整個 ControlSet，所以 ControlSet 上的原動畫在烘焙時會先拆下，烘焙後再接回動畫層下方。
- 實測環境：AdvancedSkeleton 6.755、Maya 2025；rig 用一層 namespace 參照，或沒有 namespace 的參照，兩種都測過。其他 ADV 版本會顯示提醒。
- 針對 ADV 6.755、有 namespace 的 rig，修正了兩個問題：
  - 套用時 namespace 會遺失，改由 `$gAsPanelNamespace` 帶入。
  - 手指控制器會被加上兩次 namespace，改由面板自己補上手指約束。

## 檔案

| 檔案 | 用途 |
| --- | --- |
| `flowhmr_bridge/client.py` | 和 FlowHMR 本機 API 通訊，只讀取、只連 127.0.0.1，不依賴 Maya |
| `flowhmr_bridge/timing.py` | 30 FPS 匯入、烘焙，以及完成後換幀率 |
| `flowhmr_bridge/adv.py` | ADV MoCapLibrary 套用、烘焙、清理 |
| `flowhmr_bridge/layers.py` | 把烘焙結果放進新的動畫層、暫時拆下並接回原動畫、移除動畫層 |
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
