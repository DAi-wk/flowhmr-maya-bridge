# 研究：自訂對應能多接近 FlowHMR 的角色預覽

問題：rig 走「自訂對應」時，能不能做出和 HMR 角色預覽（原始動作模式）一樣的結果？如果 ADV rig 改用自訂設定檔、定義和 HMR 相同也能一樣，其他 rig 應該也行。

這份研究的結論已經做成正式模式「自訂對應＋HMR 預覽求解」（`flowhmr_bridge/custom_preview.py`），用法和實測見主 README。下面是研究時的原型和數據。

## 做法

所有實驗都在獨立的 mayapy 裡跑，rig 用 reference 載入、從不存檔。

1. **建立靜置姿勢**：照設定檔自己的規則（`rest: zero`：對應到的控制器歸零），再套用 hold 值。
2. **探測**：每個控制器每軸轉 5°，記錄主骨架裡哪些關節的 local 旋轉跟著變、變了多少（比例），以及它們在世界空間是不是跟控制器一起剛性轉動。控制器帶動的「主要關節」，取最靠近根部、而且至少兩個軸在世界空間跟著轉的那一個。
3. **對應**：設定檔每個 slot 的控制器 → 它的主要關節，交給 FlowHMR 自己的求解器（`createFbxCharacterRetarget`，和原始動作模式同一套）。求解器跑在 rig 的蒙皮骨架上。
4. **驅動**：每個控制器跟著自己關節的目標走（follower：build pose 時的相對關係固定），烘焙後逐幀量測每根骨頭和目標的差距。

腳本放在這個資料夾，參數都從命令列傳入：

```
mayapy research/custom_hmr.py --scene RIG.ma --profile PROFILE.json --source CACHE --out RESULT.json
       [--joints J.json] [--holds H.json] [--followers F.json] [--refine N [--refine-translate]]
       [--reference HMR_CHARACTER.fbx]
python research/summarize.py RESULT.json [--probe] [--all]
```

- `--source` 是 `/api/jobs/<id>/pmx-motion` 的快取（`source_meta.json` + `source.bin`）。只用 GET，不會觸發 HMR 介面重新整理。
- `--reference` 是 HMR 自己匯出的角色 FBX，用來做和求解器無關的獨立對照。
- `adv_extras.py` 會列出 ADV rig 要宣告的 hold 和額外控制器（只用來產生 B、C 兩組實驗的輸入）。
- `hmr_suggest.mjs` 會跑 HMR 自己的自動 slot 建議。

對照組：同一環境下的原始動作模式，和 HMR 匯出相比是 0.000012 cm、0.00005°（70 個關節）。

## 結果

測試動作是一段 303 幀的影片。

### ADV 測試 rig

設定檔的 slot 定義和 HMR 相同：骨盆→RootX_M，脊椎→FKSpine1_M、FKSpine1Part2_M、FKChest_M，四肢和手指用 FK。共 74 根骨頭。

探測自動得到的 slot→關節對應和 HMR 對這個 rig 的定義**完全相同**（52 個 slot）。

| 做法 | 最大旋轉差 | 最大位置差（不含 root 關節） | root 關節 | 剩下的差 |
|---|---|---|---|---|
| A 只用通用方法（探測＋跟隨） | 59.5°（Elbow_L） | 1.28 cm（Head_M） | 24.6 cm | 肩、肘、髖的扭轉 28–59°；脊椎、頸 16–27° |
| B = A＋5 個 hold（3 個 `bias`、2 個頸部扭轉倍率） | 59.5° | 0.000 cm | 24.6 cm | 只剩四肢扭轉 |
| C = B＋23 個額外跟隨控制器（18 個 Micro、5 個 FK Part）＋`FKroot_M` | **0.0000°** | **0.0000 cm** | **0.0000 cm** | 無。和 HMR 自己匯出的角色 FBX 比也是 0.0000° / 0.0000 cm |
| D = A＋數值求解（只解 FKRoot_M、FKSpine1_M、FKNeck_M） | 59.5° | 0.86 cm | 24.6 cm | Spine1 從 16.4° 降到 8.8°；Neck_M 最大值反而從 20.2° 升到 23.7°（平均 6.9°→6.7°）；四肢扭轉不變；需 230 秒 |

A 組各關節的實際情況：
- 根部、膝、踝、腳尖、胸、肩胛、頭、手腕、所有手指都是 0.000°。
- 差距只出在 ADV 把旋轉分給 Part 關節的地方：
  - `FKShoulder`、`FKElbow`、`FKHip` 的扭轉不在本節，各 1/3 分給 Part1、Part2 和下一節；
  - `FKWrist`、`FKKnee` 的扭轉只有 1/3 留在本節，其餘分給上一段的 Part 關節；
  - 脊椎和頸的 `bias` 預設把彎曲平均分給三節。
- HMR 則讓 Part 關節保持靜置、整段旋轉都放在主關節上。

### Hive 測試 rig

使用 `zoo_hive_biped.json`（FK），手指沒有對應。這個 rig 沒有 HMR 匯出可以對照，所以目標是 HMR 求解器在這個 rig 骨架上的結果。

HMR 的自動建議認不出 `spine_00_jnt` 這類名字，52 個 slot 全是空的；在 HMR 裡也得手動對應。這裡手動指定：骨盆→spine_00、Spine→spine_01、Spine1→spine_03、Spine2→spine_05。共 25 根骨頭。

| 做法 | 最大旋轉差 | 最大位置差（不含 root 關節） | 剩下的差 |
|---|---|---|---|
| A 只用通用方法 | 20.6°（spine_05） | 3.6 cm（鎖骨以上整段跟著偏） | ribbon 脊椎；四肢、鎖骨、頸、頭是 0.000° |
| B = A＋數值求解（ctrl00–02 的旋轉） | 12.9°（spine_01） | 2.0 cm | 胸從 20.6° 降到 6.2°；需 48 秒 |
| C = B＋位移 | 12.9° | 2.0 cm | 和 B 差不多；需 90 秒 |
| D = C＋hips 一起解 | 8.0° | 2.8 cm | 脊椎變好，但骨盆偏 5.9° / 2.4 cm |

Root 關節在每一組都偏 28 cm：設定檔沒有地面 root 的 slot。

## 結論

1. **能做到完全一樣，但只限於 rig 本身有對應自由度的部分。** ADV 改走自訂，只要設定檔能描述三件事，結果就和 HMR 逐幀相同：
   - 關節對應（探測可以自動產生）；
   - 把 rig 的分段設定固定住的 hold（ADV 是 5 個值）；
   - 額外跟隨控制器（ADV 是 Micro 和 FK Part，負責分段關節）。
2. **只用通用方法時，一比一的控制器（四肢彎曲、手指、頭、鎖骨、腳）本來就完全一致。** 差距都在 rig 把旋轉分給多個關節的地方：
   - 扭轉分段（ADV 四肢）：主關節的世界方向對，只有 Part 關節的扭轉分配不同。沒有額外控制器就無解，數值求解也沒有辦法。
   - 彎曲分散（ADV 的 bias、Hive 的 ribbon 脊椎）：用這次的原型求解器（逐幀、從上一幀接續、每幀 4 次迭代），胸或下脊椎的最大差大約減半；但 ADV 的頸部最大值反而變大，也都沒到 0，而且很慢（一段要 1–4 分鐘）。Hive 的脊椎 slot 是手動挑的，換別的關節或換更好的求解器可能更接近，這次沒有測。求解範圍放太大會發散：一次解 14 個控制器時，手腕偏到 123°。
3. **Root 關節（地面 root）在兩個 rig 都缺 slot**，目前的設定檔格式沒辦法描述它。

## 設定檔格式要加的東西

| 項目 | 用途 | 能自動嗎 |
|---|---|---|
| 骨架對應（slot → 蒙皮關節） | 交給 HMR 求解器 | 已驗證：ADV 的探測結果和 HMR 相同。ribbon 脊椎要手動 |
| 額外跟隨（關節 → 控制器） | Part／扭轉關節，例如 ADV 的 Micro | 構想，未實作：掃描整個 rig，找出「只帶動一個關節、而且是一比一」的控制器 |
| hold（已有） | 分段設定，例如 bias、扭轉倍率 | 不行，要使用者填 |
| 地面 root slot | 角色整體移動的 root 關節 | 構想，未實作：用探測找 |
| 數值求解開關 | 只用在「沒有任何一軸是一比一」的控制器 | 已驗證：探測可以判斷（ADV 選出 3 個、Hive 選出 3 個） |

## 限制

- Hive 的結果是「和 HMR 求解器的目標比」，不是和 HMR 匯出比。
  - 腳底高度取自 rig 的腳部蒙皮，沒有獨立驗證。
  - 主骨架中有關節的靜置姿勢和 bind pose 差到 24°，還沒查是哪一個。若在量測的骨頭上，HMR 讀到的靜置姿勢會不同。（ADV 也有類似的差距，但它已經用 HMR 匯出驗證過。）
- 手指在 Hive 上沒有測：那個 rig 的手指走自己的 IK 層。
- 全部是烘焙成一般曲線後量的，沒有經過 `generic.apply` 和動畫層。自訂模式能不能把不可 key 的 `bias`、或不是控制器的 `twistAmountDivide*.input2` 留在動畫層上，還沒驗證：`generic` 遇到放不進動畫層的屬性只會記一行 log，原始動作模式則會直接報錯。
- 只測了一段動作和兩個 rig。
