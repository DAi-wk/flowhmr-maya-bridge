# FlowHMR Bridge for Maya

把 FlowHMR 生成好的動作套用到 Maya 裡的 rig。AdvancedSkeleton 可以直接從任務的原始 SMPL-H 動作重現角色預覽，也可以使用 ADV 動捕庫或已匯出的角色 FBX。其他 rig 可用「自訂對應」指定控制器。

> 這是非官方的 Maya 外掛，和 FlowHMR 的作者沒有關係。它不修改 FlowHMR 的任何檔案，只透過 FlowHMR 桌面版的本機 API（127.0.0.1）讀取任務、動作與角色設定；原始動作模式另外會用 Node.js 執行本機安裝的 FlowHMR 預覽程式，但不改動它。這組 API 和求解器是 FlowHMR 介面自己用的，沒有公開文件，FlowHMR 更新後可能會變動。

**English** — An unofficial Maya add-on that applies a finished FlowHMR job to a rig. The raw ADV mode runs the installed character preview solver on the original SMPL-H samples and bakes the resulting FK, IK and secondary controls onto an override layer. The panel also supports ADV's MoCapLibrary, exported character FBXs and custom rig mappings. It is not affiliated with FlowHMR's author and does not modify FlowHMR's files; the local API and solver it relies on are FlowHMR's internal web-UI code and may change between releases. Documentation below is in Traditional Chinese.

## 需求

- FlowHMR 桌面版或網頁版，和 Maya 在同一台電腦上執行（測試版本 v0.3.9）。
- Maya 2025（Python 3）。Maya 2024 應該也能用，但沒有完整測過。
- 「AdvancedSkeleton 動捕庫」模式需要 AdvancedSkeleton 本身（測試版本 6.755）。其他 ADV 模式只需要場景裡的 ADV rig，不用載入 AdvancedSkeleton；自訂模式不需要任何外掛。
- 「原始動作 → ADV」需要 Maya 的 PATH 找得到 Node.js（22.15 以上，實測 24.16）。面板上方的 FlowHMR 資料夾應指向目前使用的桌面版。

**連線埠**：面板上方的「埠號」預設是「自動」，會依序試下面幾個，用第一個有回應的 FlowHMR：
1. 桌面版記錄的埠號（FlowHMR 資料夾裡的 `.cache/desktop-port.txt`）
2. 網頁版記錄的埠號（`start-web.cmd -ServerOnly -Port <埠號>` 寫在 `logs/web-process.json`）
3. 60550（桌面版和 `start-web.cmd` 的預設值）
4. 8080（直接執行 `app.py` 的預設值）

網頁版用了其他埠號時，在「埠號」直接輸入即可，面板會記住。只會連本機（127.0.0.1）。FlowHMR 資料夾的位置可以在面板上填，或設定環境變數 `FLOWHMR_ROOT`；兩者都沒有時，會找這個 repo 旁邊名為 `FlowHMR` 的資料夾。

**AdvancedSkeleton 的位置**：只有「AdvancedSkeleton 動捕庫」模式會用到 AdvancedSkeleton 本身的程式，它解壓縮在哪裡都可以。面板依序找：
1. 這次開 Maya 後已經載入的 ADV
2. 上次找到或手動指定的位置
3. ADV 安裝時建立的工具列按鈕所指的 `AdvancedSkeleton.mel`
4. Maya 的 script 路徑
5. Maya 使用者資料夾裡的 `AdvancedSkeleton*` 資料夾

同時找到好幾份時，用版本最新的。找到後會記住。都找不到時，面板照常開啟，只有動捕庫模式不能用，可以按「目標 rig」旁的 **ADV…** 指定 `AdvancedSkeleton.mel`。

其他 ADV 模式（原始動作、角色 FBX）回到 build pose 時，不呼叫 ADV 的 `asGoToBuildPose`，而是直接讀 rig 裡 `buildPose` 節點記錄的指令，只執行其中的 setAttr 和 xform。實測在 ADV 6.755 測試 rig 上：把 421 個控制器的 3325 個通道打亂後，結果和 `asGoToBuildPose` 完全相同；ADV 沒記錄的 `FKroot_M` 等控制器，原本就另外歸零。沒有載入 ADV 時，原始動作模式和 HMR 匯出逐幀比對仍是 0.00005° / 0.00001 cm。

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

1. 啟動 FlowHMR（桌面版或網頁版），在那邊匯入影片、選人、生成。面板會自動連線，每 3 秒更新一次任務列表。
2. 在 Maya 開場景，把 rig 參照或匯入進來。rig 已經有動畫也可以，見下方「動畫層與覆蓋」。
3. 在面板中選一個「完成」的任務，再選 **rig 類型**。選單上面兩個（白字）是 HMR 預覽求解器的模式，結果和 HMR 的角色預覽相同，建議使用；下面三個（灰字）是舊版模式。
   - **原始動作 → ADV（HMR 預覽求解器）**：選目標 rig，按 **用原始動作套用到 ADV rig**。動作直接來自任務。
   - **自訂對應＋HMR 預覽求解**：任意 rig，用一份對應設定，改用 HMR 預覽的求解器，見下方「自訂對應＋HMR 預覽求解」。
   - 舊版 **AdvancedSkeleton 動捕庫**：選目標 rig，按 **套用到 ADV rig**。
   - 舊版 **自訂對應（校正對齊）**：選一份對應設定，面板會自動找出場景裡符合的 namespace，按 **套用到自訂 rig**。還沒有設定時，按 **新增…** 開啟自訂對應面板。
   - 舊版 **HMR 角色 FBX**：ADV rig 照 FlowHMR 預覽裡的角色動作，見下方「HMR 角色 FBX」。

   兩個 HMR 預覽求解模式都可以勾 **手肘只轉 Z**，見下方「手肘只轉 Z」。
4. 選完成後的幀率，按套用。FlowHMR 的動作會放在一個新的動畫層上。

### 原始動作 → ADV

每段影片的動畫都來自該任務的 52 個 SMPL-H 關節。**使用 HMR 已儲存的角色設定**（預設勾選）決定目標角色的資料來源。

- **勾選**（預設）：讀取 HMR 裡這個人物使用的靜態蒙皮角色、關節對應，以及重定向編輯器裡的「校正值」（各關節和整個角色的旋轉、平移）。要和預覽完全一致，請在 HMR 選用同一個 ADV rig 的蒙皮 FBX 並儲存設定；編輯器裡還沒儲存的修改讀不到。HMR 存的角色不是這個 rig 的蒙皮骨架時，套用會停止並提示。實測：在 Maya 裡把手肘轉 25°、頭轉 10° 並平移 3 cm、整個角色轉 20° 並移 10 cm 加進讀到的設定，rig 上分別量到 25.000°、10.000° 加 3.000 cm、20.000° 加 10.0 cm，其他關節不變。關節平移的校正需要對應的 FK 控制器平移沒有鎖住。
- **取消勾選**，或 HMR 人物使用預設 SMPL-H：只讀取任務的原始動作，HMR 的校正值不會套用。Bridge 從 Maya 的 build pose 建立標準 ADV 對應，從場景裡可見的足部蒙皮取樣鞋底。純骨架 rig 使用求解器估計的鞋底。自訂對應、骨長和鞋底幾何會影響這條路徑與指定角色預覽的差異。

Bridge 讀取預覽使用的靜置關節、逐幀世界座標與四元數、地面高度和四軌足部接觸。它呼叫同一份 `createFbxCharacterRetarget`，因此手掌、軀幹、手臂觸及距離、腳掌貼地和已儲存的姿勢微調，共用 HMR 的計算。Maya 再以 build pose 校正控制器的旋轉軸和樞紐，逐幀把姿勢烘焙到新動畫層。第 0 幀就是第一幀動作。

有分段的 ADV rig 會依控制器能力補償。Bridge 用脊椎和頸部的 FK bias 分配彎曲，用分段 FK 和 Micro 控制器重現各段姿勢與扭轉。若頸部 Skin 關節的約束還會疊加頭部扭轉，該扭轉倍率也由本次動畫層控制，靜音後回復原值。Log 會列出接上控制器、烘焙及放上最終動畫層後的抽查誤差。使用已儲存的角色時會核對關節名稱、階層，以及相對骨盆的綁定位置與旋轉。預覽編輯器中的微調須先儲存。

實測（Maya 2025、ADV 6.755、ADV 測試 rig、303 幀單人舞蹈動作）：以 HMR 匯出的角色 FBX 作獨立基準，逐幀比較最終動畫層中的 53 個動畫關節，以及同一骨鏈的 Part 關節，共 70 個。勾選或不勾選「使用 HMR 已儲存的角色設定」，世界位置最大差都在 0.00002 cm 以內，世界旋轉最大差在 0.001° 以內（浮點誤差範圍）。另一段 344 幀動作用帶 namespace 的 rig 測試，完成烘焙與動畫層抽查。

### AdvancedSkeleton 動捕庫

動捕庫模式的流程如下。
- 先把場景暫時切到 30 FPS，用 ADV MoCapLibrary 接上約束，再用 Bake Body 烘焙。
- 接著跑 euler filter，最後換到你選的幀率。key 以秒數保留時間，所以 30、60、120 fps 會落在整數幀。
- 第一幀（30 FPS 時的第 −1 幀）是 T Pose 參考。

任何一步失敗，都會清掉匯入的動捕骨架、約束、這次新增的 key 和動畫層，接回原本的動畫，並還原幀率。動畫層裡的 FKIKBlend 是 ADV 設定的值：腿 IK、手臂 FK。

ADV 接上約束後，面板會檢查第 −1 幀的 RootX_M、脊椎、頸、頭控制器是否還在 build pose（ADV 用 `-mo` 在這一幀算偏移，正常情況下完全不會動）。任何一個偏超過 5° 就停止套用並清理，不會烘出身體扭曲的結果。曾在一個連續套用、失敗過的 Maya 工作階段中發生 FKSpine1_M 偏了約 130° 的情況，重開 Maya 後就正常；遇到這個提示請存檔後重開 Maya。套用失敗時，完整的錯誤訊息會印在 Script Editor。

**套用後不能用 Ctrl+Z 復原。** ADV 匯入 FBX 的步驟不能 undo，還會清空它之前的 undo 紀錄。實測只按一次 Ctrl+Z，會把刪掉的動捕骨架帶回來，場景也會卡在 30 FPS。所以套用結束後會主動清空 undo 紀錄。要撤銷這次套用，請刪除它的動畫層（或按「移除最新的 FlowHMR 動畫層」）。

## HMR 角色 FBX（ADV rig，照 FlowHMR 預覽）

ADV 模式和自訂模式都是拿 FlowHMR 偵測到的動作自己重定向，結果和 FlowHMR 預覽裡的角色不會一樣：預覽有自己的解法（對齊鎖骨、依手長重新擺手、把手拉向頭和胸、分散脊椎、踩穩腳），手臂和偵測到的動作差 20–28°。要讓 rig 和預覽一致，建議用上面的「原始動作 → ADV」。這個模式改用從 FlowHMR 手動匯出的角色 FBX，不需要 Node.js，但 Part 關節的扭轉交給 rig 自己分配（見下方實測）：

1. 從 rig 匯出它自己的蒙皮模型（DeformationSystem 骨架加蒙皮網格）成 FBX，在 FlowHMR 預覽裡替這個人物套用它當角色，並儲存重定向。旋轉順序不是 XYZ 的關節要先處理好，見下方「FlowHMR 預覽中的 FBX 角色姿勢歪掉」。
2. 在 FlowHMR 按 **匯出 FBX…**，格式選 **目前角色 FBX**，不用勾「包含蒙皮網格」。瀏覽器會把 `<影片名>_retarget_fbx_skeleton.fbx` 存到下載資料夾。這個檔案是預覽頁面算好的，bridge 沒辦法自己下載，每次調整預覽後都要重新匯出。
3. 在面板選 **HMR 角色 FBX** 和目標 rig，再選任務：面板會在下載資料夾（或上次用「…」選檔的資料夾）找這個任務最新的角色 FBX，也可以按「…」自己選。這個模式不需要連線 FlowHMR。
4. 按 **用角色 FBX 套用到 ADV rig**。

套用時：
- 先檢查角色 FBX 的骨架是不是這個 rig 的：會動的關節名稱、父骨、旋轉順序、jointOrient、骨長都要相同，不同就停止。匯出的 FBX 沒有 bind pose，所以檢查的是骨架結構。
- rig 回到 build pose，所有 FKIKBlend 設成 FK。每個會動的關節驅動同名的 FK 控制器（`FK<關節>`，骨盆和移動由 `RootX_M` 負責，骨盆上方的 root 關節由它的 FK 控制器負責），偏移量用 build pose 時控制器和關節的相對關係。
- IK 手腳、IK 腳尖和 pole 也會跟著擺好，之後切到 IK 不會跳回 build pose；但 IK 的手肘、膝蓋只能單軸彎，扭轉會和 FK 不同。
- 接上後和烘焙後，各在 6 個幀比對 rig 關節和角色 FBX，結果寫在 log。

實測（ADV 測試 rig，303 幀，和 FlowHMR 匯出的角色 FBX 逐幀比對）：
- 角色 FBX 有動作的 53 個關節裡，45 個旋轉完全一致（0.00°），包括骨盆、root、胸、頭、鎖骨、手腕、膝蓋、腳踝、腳尖和 30 個手指關節。位置方面，骨盆、root（移動比例 1.000）和整條腿完全一致（0.00 cm）；胸口以上因為下面說的脊椎分段，整體平均偏 0.6 cm、最多 1.3 cm。對照之下，ADV 模式鎖骨差 27.8°、上臂方向平均差 21–24°。
- 另外 8 個是 rig 有分段（Part）關節的骨頭：上臂、前臂、大腿只差在沿骨頭的扭轉（上臂最多 39°、前臂 59°、大腿 30°），骨頭方向完全一致，手腕和膝蓋也完全一致。原因是 ADV 會把扭轉分散到 ShoulderPart、ElbowPart、HipPart 關節，FlowHMR 匯出時這些 Part 關節是不動的，扭轉全在主關節上。Part 關節本身也差在扭轉（最多 49°），所以上臂、前臂、大腿蒙皮的扭轉分布會和 FlowHMR 預覽不同。這是 rig 自己的蒙皮設計，不是重定向誤差；「原始動作 → ADV」會用 Micro 控制器把 Part 關節也對齊 HMR。
- 脊椎和頸部也一樣，ADV 把彎曲分散到 Spine1Part、NeckPart 關節，所以胸口以上的位置最多差 1.3 cm（平均 0.6 cm），胸、頭的旋轉仍然一致。
- IK 骨鏈：手腕、腳踝、腳尖完全一致，手肘、膝蓋位置平均差約 0.5 cm（最大 2.4 cm）。
- 選錯檔案（例如 FlowHMR 一般的 FBX）時，會在烘焙前停止並清理，場景和套用前完全相同。

限制：
- 只支援 ADV rig，而且角色 FBX 必須是同一個 rig 的蒙皮骨架。
- 每次只能匯出一個人物。
- 目前只在沒有 namespace 的 ADV rig、一段動作上實測過；有 namespace 的 rig，以及勾了「包含蒙皮網格」的匯出還沒測。

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

## 自訂對應＋HMR 預覽求解（任意 rig）

這個模式和「原始動作 → ADV」用的是同一個求解器，只是對象換成任意 rig：

- 動作來自任務的 SMPL-H 原始動作，在 rig 自己的蒙皮骨架上，用 HMR 的 `createFbxCharacterRetarget` 求解。
- 再讓對應設定裡的控制器跟著求解出來的關節。
- 需要 Node.js 22.15 以上。

步驟和「自訂對應」相同：選對應設定和 namespace，選任務，再按 **用 HMR 預覽求解套用到自訂 rig**。**使用 HMR 已儲存的角色設定** 的意思和原始動作模式一樣：勾選時讀 HMR 為這個人物存的角色、關節對應和校正值，那個角色必須是這個 rig 的蒙皮 FBX。

套用時：
1. 對應到的控制器依設定檔的靜置規則歸零，並套用 hold 值。
2. **探測**：每個控制器每軸轉 5°，看它實際帶動哪個蒙皮關節。會找最靠近根部、而且至少兩軸在世界空間一起轉的那一個。不用再另外指定骨架，ADV 測試 rig 上探測到的對應和 HMR 的定義完全相同。log 會列出控制器分成三類：
   - **一比一帶動**：結果和 HMR 一致。
   - **有一軸的扭轉分給其他關節**：例如 ADV 把上臂的扭轉分給 Part 關節。
   - **把旋轉分散到多個關節**：例如脊椎 bias、ribbon 脊椎。
3. 用 rig 的 build pose 求解。每個控制器以 build pose 時和關節的相對關係跟著目標；IK 控制器跟著手腳末端，pole 放在手肘、膝蓋彎曲的方向。
4. 烘焙到新的動畫層。接上後和放進動畫層後，會在 6 個幀比對每個求解的關節，包括沒有控制器跟著的關節，結果寫在 log。

設定檔可以加一個 `hmr` 區塊，只有這個模式會讀，舊的「自訂對應」不受影響：

```json
"hmr": {
  "joints":    {"Spine": "spine_01_jnt"},
  "followers": [["ShoulderPart1_{s}", "MicroShoulderPart1_{s}"], ["root", "FKroot_M", "parent"]],
  "hold":      {"FKSpine1_M.bias": 10}
}
```

- `joints`：直接指定某個 slot 用哪個蒙皮關節，蓋過探測結果。ribbon 脊椎這類控制器和關節不是一對一的部位要用到。值寫 `null` 表示不對應。
- `followers`：額外的控制器跟著某個關節，例如扭轉分段關節的控制器，或角色整體移動的 root 關節。第三欄 `parent` 會連位移一起跟，預設 `orient`。
- `hold`：只在這個模式固定的屬性，例如把脊椎 bias 設到讓整段彎曲留在主關節。會放進動畫層，靜音後回到原值。
- 在人形定義面板裡編輯、存檔後，`hmr` 區塊會保留下來，`{s}`、`{Side}` 會展開成實際名稱。

內建範本 **AdvancedSkeleton（HMR 預覽定義，FK）**（`profiles/adv_hmr.json`）：
- slot 定義和 HMR 對 ADV 的定義相同。
- 用 5 個 hold 固定脊椎、頸部的分配。
- 用 23 個 Micro／FK Part 控制器對齊分段關節，`FKroot_M` 跟著 root 關節。
- bias 10 是 ADV 6.755 的最大值，其他版本請確認。

**數值求解**（可選，較慢）：勾 **分散型控制器用數值求解**，探測為「分散」的控制器會逐幀求解旋轉，讓它們帶動的關節盡量貼近預覽，其他控制器照常跟著。
- 每一幀都從「跟隨」的結果出發，只接受讓整體誤差變小的解，所以不會比不勾更差。但整體誤差是所有關節的總和，個別關節有可能變差：研究時 ADV 的頸部最大差就從 20.2° 變成 23.7°。
- 只用在沒有任何一軸是一比一的控制器上；範圍放太大會發散，所以不會套到其他控制器。
- log 會寫出有幾幀比跟隨更接近。

實測（Maya 2025、mayapy、303 幀測試動作）：
- **ADV 測試 rig 加內建範本**：
  - 最終動畫層和 HMR 匯出的角色 FBX 逐幀比較：70 個動畫關節和 Part 關節 × 303 幀，最大差 0.00005° / 0.00001 cm，和原始動作模式相同。rig 用 namespace 參照或不用 namespace 直接開啟，結果都一樣。
  - 靜音動畫層後，5 個 hold 屬性回到 rig 原本的值。
  - 在同一個模式的動畫層上再套用一次，結果相同。
  - 在探測後、接上後各強制失敗一次。比對所有被驅動控制器的通道和設定、動畫層、namespace、幀率，以及場景裡的全部節點，都和套用前相同。
  - 套用一次約 19 秒，其中探測 8 秒、烘焙 9 秒。
  - 上面這組比對用的 HMR 匯出，是在 HMR 存的設定還沒有校正值時匯出的，所以驗證的是「只用 rig」這條路徑。
- **使用 HMR 已儲存的角色設定**：先套一次只用 rig 的結果，再疊上已儲存的設定，比較每個關節的變化，但沒有和 HMR 匯出直接比對。
  - 校正值：HMR 存的上臂 −13.4°，再加上合成的手肘 25°、頭 10° 和 3 cm、整個角色 20° 和 10 cm。
  - rig 上量到：上臂 13.400°、手肘 25.000°、頭 10.000° 加 3.000 cm、root 20.000° 加 10.4 cm。root 繞樞紐旋轉，位置也會跟著移一點。
  - 抽查的 27 個關節裡，其他 20 個的 local 不變。另外兩根拇指根部變了 73°，見下一點。
  - HMR 的對應如果把某個 slot 對到 rig 沒有控制器的關節，log 會列出「沒有控制器跟著」。測試時 HMR 存的拇指對到 ThumbFinger2–4（只用 rig 時是 1–3），ADV 的拇指尖沒有 FK 控制器，那一節就和預覽差 80–96°。原始動作模式也一樣。
- **ADV 測試 rig，只用通用方法**（不用 hmr 區塊，也沒有 ADV 專用的處理）：一比一的部位完全一致，包括根部、膝、踝、胸、頭、手腕和手指。上臂、前臂、大腿的扭轉差到 59°，脊椎、頸 16–27°，原因是 ADV 把旋轉分給了 Part 關節。
- **Hive 測試 rig**（Zoo Hive 範本加 `hmr.joints` 指定脊椎；沒有 HMR 匯出可對照，比的是求解器的目標；以下是 log 在 6 個幀抽查的數字）：
  - 四肢、鎖骨、頸、頭 0.000°。
  - ribbon 脊椎最大差 13.7°，脊椎以上位置偏 2.7 cm。
  - 勾數值求解後，303 幀全部比跟隨更接近：脊椎降到 7.9°（胸 6.4°），脊椎以上位置偏 1.6 cm，多 44 秒。
  - root 關節沒有對應的控制器，偏 27 cm。
  - 研究時逐幀量的最大差：只跟隨 20.6°，數值求解後 12.9°（當時的求解從上一幀接續，不是從跟隨出發）。

限制：
- 只在 ADV 和 Hive 測試 rig、一段動作上實測過，而且是在 mayapy 裡跑的；Maya 面板裡和 parallel evaluation 下還沒實際套用過。
- `adv_hmr.json` 也會出現在舊「自訂對應」的設定清單裡。在那個模式下，`hmr` 區塊不會被讀取。
- IK 手腳跟著求解的手腳末端擺放，但 IK 只能單軸彎，手肘、膝蓋的扭轉會和預覽不同；要完全一致請讓四肢顯示 FK。
- 研究過程與完整數據在 `research/README.md`。

## 手肘只轉 Z（可選）

動畫師 key 手肘通常只轉一個軸（ADV 是 Z）。HMR 預覽求解出來的手肘，除了彎曲還帶扭轉和外擺：在 ADV 測試 rig 上平均離「只轉 Z」約 50°。

勾選 **手肘只轉 Z** 後，烘焙完會逐幀改寫 FK 手臂：
- 手肘控制器只留 Z，X/Y 改成上臂沿骨頭的扭轉，再加上 Z 彎曲。
- 肩、肘、腕的位置不變。
- 手腕控制器的世界旋轉照原本的，所以手掌方向不變。
- 上臂、前臂的扭轉改由 rig 自己分配，和 HMR 預覽不同。所以上臂、前臂的 Micro／跟隨控制器在這個選項下不會套用。

計算方式參考獨立的 IK/FK 切換工具裡的「清除肘／膝 X/Y」，只讀寫控制器和關節，不呼叫 AdvancedSkeleton 的程式。

彎曲軸會從動作判斷：在手肘控制器自己的座標裡，看手臂實際繞哪一軸彎，ADV 是 Z。手肘以別的軸彎曲的 rig 會改成只轉那一軸，例如 Hive 測試 rig 是 Y。以下情況會略過那隻手臂，log 會寫原因：
- 判斷不出明確的彎曲軸（主軸不到 60%）。
- 上臂、前臂、手腕的旋轉被鎖住。
- 用了非等比縮放。

實測（Maya 2025、mayapy、303 幀測試動作）：

| rig | 彎曲軸 | 手肘其他兩軸（最終動畫層） | 上臂多轉（左／右） | 和 HMR 比 |
|---|---|---|---|---|
| ADV 測試 rig（原始動作模式，有／沒有 namespace；自訂＋HMR 加內建範本） | Z | 0.00000° | 最多 48° / 53° | 和 HMR 匯出的角色 FBX 逐幀比，70 個關節的位置 0.00001 cm；手腕、手指和其他關節的旋轉 0.00005°；只有 12 個上臂、前臂扭轉關節不同（最多 46°） |
| Hive 測試 rig（自訂＋HMR） | Y（自動判斷） | 0.00000° | 最多 39° / 44° | 修正前後，肩、肘、腕的位置沒有變 |

研究過程（為什麼是這三個量、差距多大）見 `research/README.md` 的「前臂（FK 手肘）只轉 Z 軸」。

## 動畫層與覆蓋

每次套用都會建立一個新的**覆蓋（override）動畫層**，名稱是 `FlowHMR_<namespace>_<影片名>`，FlowHMR 的動作全部在這一層上：

- 把這一層**靜音**或權重調成 0，就看到原本的動畫；調成 0–1 之間可以混合。
- 再套用一次會疊一個新的動畫層在上面，舊的動畫層不會被改到。
- 下層 FlowHMR 動畫層動到、但這次套用不會烘焙的通道（例如原始動作模式的脊椎 `bias`、頸部扭轉倍率和 Micro 控制器，換成自訂對應時沒有對應到的控制器），會在套用前設回「沒有任何 FlowHMR 動畫層時」的值，並放進新的動畫層。所以不管換哪一種模式疊上去，結果都和直接套在 rig 上一樣；把新的動畫層靜音，就回到下層的結果。這些通道如果在 FlowHMR 動畫層下方有你自己的動畫，就不會被改動，log 會列出來。實測：在原始動作的動畫層上疊 ADV 動捕庫、角色 FBX、自訂對應、原始動作，結果都和直接套用相同（旋轉 0.001° 以內、位置 0.0 cm）。
- 動畫層裡除了烘焙的曲線，也會記錄這次套用改變的值，例如 FK/IK 切換、靜置姿勢、ADV 的 build pose。所以只開這一層就是完整的結果，靜音後也會回到原本的設定。少數屬性（例如 proxy 切換屬性）不能放進動畫層，會直接設定，log 會列出來。
- ADV 的 build pose 記錄有時會漏掉控制器（測試 rig 漏了 root 關節的 `FKroot_M`），`asGoToBuildPose` 不會動它們，拆下動畫後它們會停在上一層第一幀的值。ADV 的三種模式回到 build pose 後，都會把這些控制器的位移、旋轉、縮放歸回預設值，log 會列出來；否則在已有 FlowHMR 動畫層的 rig 上重複套用時，root 會偏掉或套用失敗。

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

## FlowHMR 預覽中的 FBX 角色姿勢歪掉

這不是 bridge 的問題，但會讓 FlowHMR 預覽和 Maya 的結果看起來不一致：FlowHMR 0.3.9 內建的 three.js `FBXLoader` 套用 PreRotation（Maya 的 jointOrient）時，用的是關節自己的旋轉順序；FBX 規定 PreRotation 一律是 XYZ。所以旋轉順序不是 XYZ、而且 jointOrient 不只一軸的關節（ADV rig 的 Hip、Shoulder、Scapula、Wrist 常見）會被讀歪，例如腿往後彎、手舉高，預覽裡的角色也會因此偏低。

解法擇一：
- 修改 `FlowHMR/tools/web/static/vendor/three/fbx/loaders/FBXLoader.js`：`generateTransform` 與 `generateRotationTrack` 裡 PreRotation／PostRotation 的旋轉順序改用 `'ZYX'`（three.js 對應 FBX 的 XYZ），共 4 處，再重新整理 FlowHMR。FlowHMR 更新時可能被覆蓋。
- 或在匯出 FBX 前，把這些關節的旋轉順序改成 XYZ（Maya `xform -preserve` 可保持姿勢不變）。

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
| `flowhmr_bridge/ui.py` | Maya 主面板與動作來源選擇 |
| `flowhmr_bridge/raw.py`、`preview_solve.mjs` | 原始 SMPL-H 到 ADV 控制器，使用安裝版的 HMR 角色求解器 |
| `stage_raw_test.py` | 獨立 mayapy 驗證，可用已匯出的角色 FBX 逐幀比對最終動畫層和 Part 關節 |
| `flowhmr_bridge/hinge.py` | 手肘只轉 Z：把烘焙好的 FK 手臂改成手肘只轉 Z，位置和手掌方向不變 |
| `flowhmr_bridge/custom_preview.py` | 自訂對應＋HMR 預覽求解：探測控制器、在 rig 蒙皮骨架上求解、讓控制器跟著、烘焙並比對 |
| `stage_custom_hmr_test.py` | 上面這個模式的 mayapy 驗證：和角色 FBX 逐幀比對、靜音後的設定、疊套用、強制失敗 |
| `research/` | 「自訂對應能多接近 HMR 預覽」的研究腳本與結果 |
| `flowhmr_bridge/character.py` | HMR 角色 FBX 模式：匯入 FlowHMR 匯出的角色 FBX、檢查骨架、讓 ADV 的 FK／IK 控制器跟著、烘焙並比對 |
| `flowhmr_bridge/mapping_ui.py` | 人形定義面板（身體圖、手指圖、清單） |
| `flowhmr_bridge/mapping.py` | 人形定義的部位欄位、必要部位、讀寫、鏡像、自動偵測規則，不依賴 Maya |
| `tests/test_automap.py` | 自動偵測測試（ADV、Hive、mGear 命名）：`python -I tests/test_automap.py -v` |
| `tests/test_mapping.py` | 對應設定讀寫測試：`python -I tests/test_mapping.py -v` |
| `phase0_adv_test.py` | 呼叫同一個 `adv.apply()`，並量測重定向誤差和烘焙漂移 |
| `flowhmr_bridge/generic.py` | 通用引擎：依對應設定把 FlowHMR 的 `flowhmr` FBX 套到任意 rig 的 FK 控制器，含靜置姿勢校正 |
| `flowhmr_bridge/profiles/*.json` | 對應設定檔。`adv_generic_test.json` 是拿 ADV rig 測試通用引擎用的；`adv_hmr.json` 是 ADV 的 HMR 預覽定義 |
| `stage1_generic_test.py` | 第 1 階段測試：通用引擎和 ADV 結果對照、手臂垂下的靜置姿勢、失敗清理 |
| `stage_generic_test.py` | 不限 rig 的測試腳本，量測用的骨頭對應寫在設定檔的 `test` 區塊 |
| `flowhmr_bridge/profiles/zoo_hive_biped.json` | Zoo Hive 的身體和四肢 FK 對應。手指不在裡面，因為測試用的 Hive rig 手指由自訂的 IK 系統驅動 |
| `tests/test_client.py` | client 測試，FlowHMR 沒開時會略過連線部分：`python -I tests/test_client.py -v` |

測試不需要 Maya，任何 Python 3.7 以上都可以跑，包括 FlowHMR 附的 `runtime/python.exe`。`phase0_adv_test.py`、`stage*_generic_test.py` 要在 Maya 裡、拋棄式的場景中執行，會對 rig 套用和烘焙動畫。

## 參與貢獻

歡迎回報問題和開 PR，請先看 [CONTRIBUTING.md](CONTRIBUTING.md)（附英文版）。

## 授權

MIT，見 [LICENSE](LICENSE)。FlowHMR、AdvancedSkeleton、Zoo Tools、mGear 各自依它們的授權條款，這個 repo 不包含它們的任何檔案。
