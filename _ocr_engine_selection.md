# 交易行"读价格数字"OCR 引擎选型（实测版）

> 结论先行，数据全部现场跑出。**环境**：Windows 11 26200（Win10/11 行为一致）/ AMD Ryzen 7 7800X3D 8C16T / 31.7GB / **纯 CPU**（RTX 3070 存在但全程未参与；Paddle 强制 `device=cpu`）。
> **代码**：只读引用 [core/ocr.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr.py)、[core/ocr_ppocr.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr_ppocr.py)、对照工程 `C:\Users\26346\Desktop\dzd`（原"新建文件夹"，字模方案在此）。**`dafors` 业务代码零改动**，仓库里只多了本文与 `_ocr_bench_user_shots.md` 两个新建 md。
> **原始数据**：`C:\Users\26346\Documents\Trae-CN\temp_20261106_06\ocrbench\out\*.json`；脚本同目录。

---

## 一、结论（3 条）

1. **主引擎 = RapidOCR（RapidAI/RapidOCR，内置 PP-OCRv6 small ONNX 检测+识别，29.8MB）**。
   两套基准全对：主基准 36/36 ×2 种尺度、用户 5 张真实截图 42/42 ×2 种尺度，**0 误识、0 拒识**；ROI 单条 12.9~73ms，整页 678ms；`onnxruntime + opencv + numpy` 三个纯 pip 依赖，Windows CPU 直接跑，Apache-2.0，最近一次 push 2026-11-01。
   **但成绩建立在三个必须写进代码的调参上**：`Global.use_preprocess_img=False`、`Det.limit_side_len=64 / limit_type=min`、`Det.box_thresh=0.5`。用默认参数会掉到 18/36 且 1.2s/帧——这是本次实测最值钱的一条。
2. **兜底 = PP-OCRv6 tiny（PaddleOCR，6.3MB，Apache-2.0）**，**必须 3× 放大后再识别**：scale=3 时主基准 36/36、用户图 42/42，25~34ms；scale=1 时用户图的单字货币（`0`/`8`）有 8 次漏检。代价是 Paddle 依赖与 4.3s 冷启动，所以只放在"Rapid 分数 <0.5 或格式校验失败"时二次确认。
3. **自建模板字模不保留为主路径**，降级为"零依赖极速路径 / 交叉校验"：0.08MB、3.2~5.5ms、主基准 32/36（0 误识），但用户 5 图只有 24~28/42，且**噪声块会编造数字（负样本只拒掉 4/8）**。Tesseract 虽然 8.4ms 且主基准全对，但**在红字详情价上读错成 `1,888,888`**、`MeanTextConf` 对正确读数也常返 0（无法做拒识闸门），不进主链路。

---

## 二、候选对比

| 方案 | 仓库 / 版本 | License | 最近提交 | ★ | 模型体积 | 安装依赖 | 需联网 | Win+CPU | 中文 | 稳定性（缺模型/异常时） | 维护 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 自建模板字模 | `dzd\assets\digits\real`（320 字形 PNG） | 自建，无第三方 | 本地 | — | **0.08MB** | numpy+opencv | 否 | 是 | 否（只有 0-9 与 `,`） | 缺字形文件直接加载失败；无模型概念 | 取决于自己 |
| **RapidOCR** | RapidAI/RapidOCR | Apache-2.0 | 2026-11-01 | 8401 | **29.8MB**（det 9.5 + rec 20.3 ONNX） | `rapidocr`+`onnxruntime`+opencv+numpy | 仅缺模型时 | 是 | 是（有中文 rec） | 缺 ONNX 抛异常，可 try/except 降级 | 活跃 |
| PaddleOCR PP-OCRv6 tiny | PaddlePaddle/PaddleOCR | Apache-2.0 | 2026-09-16 | 34444 | **6.3MB**（1.9+4.4） | `paddlepaddle`+`paddleocr`（≈4.3GB venv） | 首次需拉模型（可预置） | 是 | 是 | 模型缺失时会尝试下载，离线且无缓存则失败 | 活跃 |
| PaddleOCR PP-OCRv6 small | 同上 | Apache-2.0 | 同上 | 同上 | 30.1MB（9.6+20.5） | 同上 | 同上 | 是 | 是 | 同上 | 活跃 |
| PaddleOCR PP-OCRv5 mobile | 同上 | Apache-2.0 | 同上 | 同上 | 21.1MB（4.7+16.4） | 同上 | 同上 | 是 | 是 | 同上 | 活跃 |
| PaddleOCR PP-OCRv5 server | 同上 | Apache-2.0 | 同上 | 同上 | 165.6MB（84.3+81.3） | 同上 | 同上 | 是（但 0.5~0.7s/条） | 是 | 同上 | 活跃 |
| EasyOCR | JaidedAI/EasyOCR | Apache-2.0 | 2024-08-14 | 23414 | craft 79.3 + en 14.4 + zh 20.3 = **114MB** | `torch(+cpu)` 约 941MB venv | 首次需下载权重 | 是 | 是（zh_sim） | 缺权重报网络错误，需手工放 `~/.EasyOCR` | **放缓**（近两年无提交） |
| Tesseract 5 | tesseract-ocr/tesseract + `tesserocr` | Apache-2.0 / MIT | 2026 | 42694 | eng 3.92 + chi_sim 2.35 = **6.3MB** | `tesserocr`（自带 libtesseract）+Pillow | 否 | 是 | 是 | tessdata 缺失时构造即抛错 | 稳定但慢更新 |
| Windows OCR (winrt) | 系统组件 | 随 Windows | — | — | 0（系统内置） | `winrt-*` | 否 | 是 | 是 | 语言包缺失时 `try_create_from_language` 返回 None | 跟系统 |
| 自训 CNN → ONNX | 本次自建（320 字形训练 + held-out 验证） | 自建 | 本地 | — | **95.2KB** | `onnxruntime`+opencv+numpy | 否 | 是 | 否 | 缺模型抛异常 | 取决于自己 |
| PaddleOCR-VL（0.9B VLM） | 同 PaddleOCR 项目 | Apache-2.0 | 2026-01 | — | 2GB 级 | paddle + VLM 运行栈 | 是 | 理论可，实测需 GPU 级算力 | 是 | 不适合 | 活跃 |
| dots.ocr | studio-dots-ai/dots.ocr | MIT | 2026-03-24 | 9186 | VLM 级 | 重 | 是 | 否（需 GPU） | 是 | — | 活跃 |
| MMOCR | open-mmlab/mmocr | Apache-2.0 | 2024-11-27 | 4746 | 视模型 | mmcv 全家桶 | 是 | 是 | 是 | — | 放缓 |
| docTR | mindee/doctr | Apache-2.0 | 2026-11-03 | 6417 | 视模型 | tensorflow/torch | 是（下权重） | 是 | 部分 | — | 活跃 |
| Surya | datalab-to/surya | Apache-2.0（此前 GPL-3.0，已换） | 2026-10-29 | 21573 | 大（多任务） | torch | 是 | 需 GPU 才实用 | 是 | — | 活跃 |

> 表格只列"价格行 ROI"这一层的可用性；PaddleOCR-VL / dots.ocr / MMOCR / docTR / Surya **未实测**（见第六节），仅作纸面排除：它们的定位是整页文档/版面理解，单行数字 ROI 上性价比不可能打过 30MB 的 PP-OCR 小模型。

### 许可证与商用

- 全部实测引擎（RapidOCR / PaddleOCR / EasyOCR / Tesseract / ONNX Runtime）均为 **Apache-2.0 或 MIT**，商用无附加限制；PP-OCR 系列权重随项目 Apache-2.0 发布；tessdata_fast 同为 Apache-2.0。
- Windows OCR 是系统组件，**不可随安装包分发**（只能在 Windows 上调用），这也它被判死的另一个原因。
- 自建字模与自训 CNN 无第三方授权问题（素材来自本游戏截图，注意游戏素材本身的二创边界，与引擎选型无关）。

---

## 三、主基准实测（20 条样本，统一 ROI + 统一预处理）

**样本构成**（`ocrbench\dataset\`，labels.json 里有逐条真值）：

- A 组 9 条：`dzd\tests\fixtures\real_digits\` 的人工标注夹具（42,888 / 13,000 / 25,000 / 20,000 / 14,000 / 13,000 / 20,000 / 2,050 / 9,999,999）。
- B 组 3 条：从 `dafors\_shots\drv_onsale.png` 手切（`38,888` 列表卡 / `2,049` 顶栏 / `38,888` 详情面板红字）。裁切坐标由列投影校准，`2,049` 那条的宽度定在 86px——把右侧另一字段的 `● 0 +` 排除在外，否则 `last_number` 会取到 `0`，测的就不是"价格字段"了。
- C 组 6 条：**合成**（`76,000 / 6,789 / 17,600 / 67,000 / 6,000 / 7,700`）。真实截图里**完全没有 6 和 7**（已逐字符核过），所以 6/7 只能用"真实字形变体 + 真实截图取样的深色底"做 alpha 合成。这一点必须如实标注：**C 组的背景是真实的，字形是真实的，排布是合成的**。
- D 组 2 条负样本：纯色空条 / 随机噪声条，期望"拒识"。

**预处理**：scale=1 原图；scale=3 为 `cv2.resize(INTER_CUBIC)`；每条重复 3 次取中位；2 轮（第 2 轮打乱顺序）。

| 引擎 | scale | 全等 | 误识 | 拒识 | 字符级准确率 | 中位 ms | p95 ms | 最差 ms | 冷启动 ms | 负样本拒识 | 两轮一致 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 自建字模 | 1 | 32/36 | 0 | 4 | 0.889 | 4.3 | 9.2 | 9.6 | 102 | 4/8 | 40/40 |
| 自建字模 | 3 | 32/36 | 0 | 4 | 0.889 | 5.5 | 9.0 | 11.7 | 102 | 4/8 | 40/40 |
| **RapidOCR(v6 small)** | 1 | **36/36** | 0 | 0 | **1.000** | 65.8 | 104.7 | 111.9 | 1218 | 8/8 | 40/40 |
| **RapidOCR(v6 small)** | 3 | **36/36** | 0 | 0 | **1.000** | 73.0 | 165.2 | 183.9 | 1218 | 8/8 | 40/40 |
| PP-OCRv6 tiny | 1 | 36/36 | 0 | 0 | 1.000 | 25.3 | 30.6 | 32.4 | 4338 | 8/8 | 40/40 |
| PP-OCRv6 tiny | 3 | 36/36 | 0 | 0 | 1.000 | 34.2 | 71.8 | 72.3 | 4338 | 8/8 | 40/40 |
| PP-OCRv6 small | 1 | 36/36 | 0 | 0 | 1.000 | 70.8 | 81.3 | 83.0 | 4601 | 8/8 | 40/40 |
| PP-OCRv6 small | 3 | 34/36 | 0 | 2 | 0.944 | 89.7 | 172.6 | 179.2 | 4601 | 8/8 | 40/40 |
| PP-OCRv5 mobile | 1 | 36/36 | 0 | 0 | 1.000 | 124.1 | 131.4 | 133.9 | 4500 | 8/8 | 40/40 |
| PP-OCRv5 mobile | 3 | 32/36 | 2 | 2 | 0.921 | 139.1 | 297.9 | 318.8 | 4500 | 8/8 | 40/40 |
| PP-OCRv5 server | 1 | 36/36 | 0 | 0 | 1.000 | 503.3 | 619.6 | 625.5 | 4576 | 8/8 | 40/40 |
| PP-OCRv5 server | 3 | 36/36 | 0 | 0 | 1.000 | 706.5 | 1518.5 | 1521.1 | 4576 | 8/8 | 40/40 |
| Tesseract5（阈 0.6） | 1 | 16/36 | 0 | 20 | 0.444 | 9.2 | 11.3 | 12.2 | 92 | 8/8 | 40/40 |
| Tesseract5（阈 0.6） | 3 | 10/36 | 0 | 26 | 0.278 | 17.7 | 26.1 | 27.8 | 92 | 8/8 | 40/40 |
| Tesseract5（无置信度门限） | 1 | 36/36 | 0 | 0 | 1.000 | 8.4 | 10.4 | 11.0 | 77 | 8/8 | 40/40 |
| Tesseract5（无置信度门限） | 3 | 36/36 | 0 | 0 | 1.000 | 14.3 | 23.0 | 26.3 | 77 | 8/8 | 40/40 |
| EasyOCR（仅 en 权重） | 1 | 36/36 | 0 | 0 | 1.000 | 32.0 | 52.2 | 53.1 | 4231 | 8/8 | 40/40 |
| EasyOCR（仅 en 权重） | 3 | 34/36 | 2 | 0 | 0.968 | 77.4 | 228.1 | 229.3 | 4231 | 8/8 | 40/40 |
| Windows OCR (winrt) | 1 | 2/36 | 4 | 30 | 0.122 | 0.9 | 2.2 | 2.3 | 106 | 8/8 | 40/40 |
| Windows OCR (winrt) | 3 | 0/36 | 28 | 8 | 0.432 | 5.1 | 7.5 | 8.2 | 106 | 8/8 | 40/40 |
| 自训 CNN（阈 0.90） | 1 | 0/36 | 0 | 36 | 0.000 | 0.6 | 1.0 | 1.5 | 135 | 8/8 | 40/40 |
| 自训 CNN（阈 0.90） | 3 | 0/36 | 0 | 36 | 0.000 | 1.2 | 3.2 | 3.8 | 135 | 8/8 | 40/40 |
| 自训 CNN（阈 0） | 1 | 14/36 | 20 | 2 | 0.647 | 0.7 | 1.0 | 1.6 | 131 | 8/8 | 40/40 |
| 自训 CNN（阈 0） | 3 | 16/36 | 18 | 2 | 0.711 | 1.6 | 4.0 | 5.0 | 131 | 8/8 | 40/40 |

**读法与坑（实测结论，不是推测）**

- **Tesseract 的置信度不可用**：正确读数（如 `42,888`）的 `MeanTextConf` 也返回 0.00。带 0.6 阈值 → 26/36 被误拒；去掉阈值 → 36/36 全对但**没有任何拒识能力**。它在用户 5 图上还把红字价读成 `1,888,888`。
- **Windows OCR 判死**：按整词/整行语言模型输出，会插空格与中文（`的 42 ， 888`），数字行直接崩。
- **自训 CNN 的瓶颈在切分不在分类**：字形分类器在 held-out（4 个来源截图留出）**77/77 = 100%**，ONNX 仅 95.2KB、0.6ms；但整行切分把金币图标、粘连数字切开错了，整行只有 14~16/36。要走这条路得重做"垂直投影 + 图标抑制 + 粘连拆分"，成本大于换引擎。
- **自建字模的两类失败**：① 字号/位置漂移（`38,888` 列表卡直接空输出，详情面板红字被 HSV 饱和过滤掉，读到 `02121`、分数 0.463 拒识）；② **噪声块编造数字**（`475,4`，负样本只拒 4/8）——这是"读不到就拒识"要求下最致命的缺陷。
- **PP-OCRv6 small 在 scale=3 反而掉了 2 条**（`2,049` 被拆成 `2,049` + `区`，分数 0.296）；v5 mobile 在 scale=3 把 `9,999,999` 拆成 `9,999` / `9,999` → `last_number` 取到 999999。
- 冷启动一列存在**磁盘缓存效应**：字模首次 1330ms / 缓存后 102ms；EasyOCR 首次 33.5s（含联网校验与冷盘读 114MB 权重）/ 缓存后 4.23s。报告按"缓存后"列，并把首次值在此说明。

---

## 四、用户 5 张真实截图的追加基准

完整表格（逐图逐串、分数、遮挡处理、整页耗时）见 **[_ocr_bench_user_shots.md](_ocr_bench_user_shots.md)**。摘要：

| 引擎 | 可评估 21 条 × 2 轮 | 误识 | 拒识 | 中位 ms(scale1/3) | 唯一读错点 |
|---|---|---|---|---|---|
| **RapidOCR** | **42/42 · 42/42** | 0 | 0 | 12.9 / 15.3 | — |
| PP-OCRv6 tiny | 34/42 · **42/42** | 0 | 8 / 0 | 22.7 / 31.2 | — |
| EasyOCR(en) | 34/42 · **42/42** | 0 | 8 / 0 | 35.0 / 71.5 | — |
| Tesseract5 | 40/42 · 36/42 | **2 / 2** | 0 / 4 | 5.3 / 9.1 | 红字价 `1,000,000` → `1,888,888` / `1,688,688` |
| PP-OCRv6 small | 38/42 · 38/42 | 0 | 4 / 4 | 67.9 / 83.9 | — |
| PP-OCRv5 mobile | 36/42 · 38/42 | 0 | 6 / 4 | 120.2 / 130.1 | — |
| 自建字模 | 24/42 · 28/42 | 2 / 2 | 16 / 12 | 3.2 / 3.8 | `1,000,000` 读成 `1` |

两轮打乱顺序后，**所有引擎 × 21 条 × 2 尺度文本 100% 一致**（21/21）。

这一组**不改变排序**，但把三条工程约束钉死了：单字货币必须 3× 放大；价格必须过千分位分组校验（RapidOCR 对被遮挡的 `9,9` 给了 0.9835 的高分，光看分数会当成价格）；红字/低对比 ROI 需要独立回归用例。

---

## 五、落地建议

### 1. 接入点（最小改动）

现有链路：`core/ocr.py::_client()` → `ocr_ppocr.make(settings["ppocr"])` → HTTP `localhost:8770`（服务由 `core/ocr_service.py::Supervisor` 拉起）。上层 `read_text / read_words / last_number / digit_runs` 是稳定契约。

- **方案 A（推荐，零 HTTP）**：新增 `core/ocr_rapid.py`，实现与 `ocr_ppocr.Backend` 同形的鸭子接口（`make(settings)`、`read_text(img)`、`read_words(img)`、`available()`、`probe()`、`name()`、`stats_line()`），在 [core/ocr.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr.py) 的 `_client()` 里按 `settings["ocr"]["backend"]`（新增键，默认 `"ppocr"`）选择。上层逻辑零改动，进程内推理省掉 8770 的 HTTP 往返。
- **方案 B（更保守）**：保持 8770 契约不变，只把 `core/ocr_service.py` 拉起的**服务端实现**换成 RapidOCR 版（`_post` / `_parse` 不动）。风险最小，但要维护一个进程。

### 2. 依赖与参数

```powershell
pip install rapidocr onnxruntime opencv-python numpy   # 合计约 60~80MB（含 onnxruntime）
```

```python
RapidOCR(params={
    "Global.use_preprocess_img": False,   # 关键：默认 True 会把 86px 的小 ROI 拉到 736，漏检且 1.2s/帧
    "Global.use_cls": False,
    "Global.text_score": 0.5,
    "Det.limit_side_len": 64, "Det.limit_type": "min",
    "Det.thresh": 0.3, "Det.box_thresh": 0.5, "Det.unclip_ratio": 1.6,
})
```

### 3. 离线模型分发

RapidOCR 的 `PP-OCRv6_det_small.onnx`(9.5MB) + `PP-OCRv6_rec_small.onnx`(20.3MB) 直接随仓库分发（`core/assets/rapid/`），用 `Det.model_path` / `Rec.model_path` 指过去；**首次运行不联网**。相比 Paddle 的 `~/.paddlex/official_models` 隐式缓存，这个路径可控、可打包。

### 4. 缺模型降级（三级）

1. 启动时校验：`onnxruntime` 可 import + 两个 onnx 文件存在且大小 > 8MB → 用 RapidOCR；
2. 否则回落到现成的 PP-OCR 服务（`ocr_ppocr`，行为不变）；
3. 再失败 → 自建字模（4ms，只认数字，必须带"空/噪声闸门"）；
4. 全不可用 → `available()=False`，上层按现有逻辑跳过，**不要用猜的值**。

### 5. 拒识策略（两道闸门，缺一不可）

- 闸门 1：`rec_score < 0.5` → 拒识。
- 闸门 2：**格式校验**——去掉逗号后纯数字、位数 1~9、千分位必须严格 3 位一组（`9,9` 这种半截串会被这里拦掉）、结尾必须是数字。对 `,` 与 `_`/换行造成的拆分，先在 ROI 内做"跨行合并"再用 [core/ocr.py](file:///C:/Users/26346/Desktop/project/dafors/core/ocr.py#L321-L339) 的 `last_number`。
- 两道不过 → 换 scale=3 重试一次；仍不过 → 返回 None（拒识）。

### 6. 需要新增的测试

1. `tests/fixtures/price_roi/`：把本次 21 条用户图 ROI 直接固化为夹具（含红字价、单字货币、被遮挡的半截串、跨行大数 `9,999,999`）。
2. 负样本必须拒识：纯色条、噪声条（字模当前 4/8 失败，回归要盯死）。
3. 尺度不变性：同一条 ROI 在 scale=1/3 结果一致（`2,049` 在 v6 small scale=3 会多出 `区`）。
4. 缺模型降级路径：删掉 onnx 后 `available()` 必须为 False，且不抛未捕获异常。
5. 冷启动预算：首次 <1.5s（Rapid 1.2s、PP-OCR 4.3s、EasyOCR 4.2~33.5s）。

---

## 六、没测到的（如实说明）

- **GPU**：全程 CPU。RTX 3070 未参与，所以 PP-OCRv5 server / EasyOCR / VLM 类的 GPU 数字没有；GPU 下 PP-OCR 通常能到 10~20ms 级，但那不是本项目的目标环境（"无 GPU 也能用"是硬约束）。
- **4K / 2K / DPI 缩放 / 窗口缩放**：只测了 1920×1080（5 张用户图全为 1080p）。游戏内 UI 缩放、字体渲染差异没覆盖；ROI 坐标在不同分辨率下需要重标。
- **中文品阶/成色/弹种名**：本次只测数字。字模方案本身没有中文（`core/glyphs.py` 的中文走的是另一条字形匹配），引擎侧的中文能力只有"理论支持"，没有做中文准确率基准。
- **EasyOCR 中文**：`zh_sim_g2.zip` 下载反复 TLS 失败（curl exit 35），最终只跑英文权重（数字识别仍有效，但中文未验证）；它的 941MB 依赖与 4.2s 冷启动已足以判否。
- **PaddleOCR-VL / dots.ocr / MMOCR / docTR / Surya**：仅纸面评估，未安装未跑。理由：目标算力是"无 GPU 的 Windows 游戏机 + 几十毫秒"，这五者是整页文档/版面模型，量与延迟都不在一个量级；`PaddleOCR-VL` 权重 2GB 级。
- **PaddleOCR 3.x + MKLDNN**：本机开 `enable_mkldnn=True` 会报 oneDNN 不支持当前 PIR 属性，全程关闭；开着能快多少没测。
- **RapidOCR 的其他模型组合**：只测内置 v6 small（det+rec）。它的 v5 mobile/server ONNX 没测；`Global.use_preprocess_img` 默认值的坑是在真实 ROI 上踩出来的，其他参数组合未穷举。
- **字模的"重训"路线**：没有尝试扩大字形库或加入多尺度模板，所以"字模能不能救回来"只有推测（需要多尺度匹配 + 噪声闸门，工作量不小）。
- **计时口径**：单条耗时为进程内 `perf_counter` 包住一次 `run()`（含 det+rec，不含解码 PNG）；冷启动为构造引擎对象的时间（首次含磁盘/网络）。机器为桌面 CPU 且跑在后台无负载，实际游戏运行时会有 ±30% 波动。

---

## 七、落地记录（2026-11-06）

选型结论已经进代码，不是只停在报告里。

### 7.1 改动清单

| 文件 | 作用 |
| --- | --- |
| `core/ocr_rapid.py`（新增） | RapidOCR 主引擎。三参数写死，模型路径按 `__file__` 解析到 `core/assets/rapid` |
| `core/ocr_chain.py`（新增） | 三级降级 + 两道闸门编排，`read_price` 的唯一实现 |
| `core/ocr.py` | `_client()` 换成主链；`last_number()` 改走严格格式闸门；新增 `read_price()` |
| `core/engine.py` | `_bar_price` 从「3× 放大 + `read_text` + `last_number`」改成 `ocr.read_price` |
| `core/digit_ocr.py`（新增） | 自建字模第三级；字模目录指向 `core/assets/digits`（342 个模板） |
| `core/ocr_service.py` | 服务端流水线优先读 `tools/OCR_tiny.yaml` |
| `tools/OCR_tiny.yaml`（新增） | PP-OCRv6 tiny 服务流水线（det 64/min、box_thresh 0.5） |
| `core/config.py` | `ocr` 段拆成 `rapid` / `ppocr` / `service` 三段；`config.json` 未改 |
| `core/assets/rapid/*.onnx`（新增） | 离线模型 9.5MB det + 20.3MB rec，共 30MB |
| `core/assets/digits/`（新增） | real 字模 + 渲染兜底，342 个模板 |
| `tests/test_ocr_price.py`（新增） | 七类验收，47 条 |
| `tests/fixtures/price_roi/`（新增） | 21 条真实价格 ROI + `manifest.json` |
| `core/cards.py` | `read_card` 默认整卡一次 `read_words`（`_whole_rows`）；新增 `read_card_bottom`（只读卡片底部 38%） |
| `core/market.py` | `_scan_cards` 补认合并为一次底带调用；`Scanner.scan` 加缩略图静止帧缓存；默认 upscale 3→1 |
| `tests/test_ocr_grade.py`（新增） | 中文品阶/成色 29 条 × 2 档 + 完整性 + 近似名拒识，4 条 |
| `tests/fixtures/grade_roi/`（新增） | 29 条真实品阶/成色 ROI（4 品阶词 / 20 成色卡 / 5 负样本）+ `manifest.json` |
| `sjz.spec` | `datas += collect_data_files("rapidocr")`；`onnxruntime` + `collect_submodules("rapidocr")` 进 hiddenimports |

### 7.2 三个必改参数写在哪

在 `core/ocr_rapid.py` 的 `_PARAMS`（模块 docstring 同时写了后果）：

```python
"Global.use_preprocess_img": False,   # 默认 True：小 ROI 被拉到 max_side，
                                      # 20 条基准漏检 2 条，单帧 70ms → 1.2s
"Det.limit_side_len": 64,             # 默认 736：小图放大 10 倍以上，费时且糊笔画
"Det.limit_type": "min",
"Det.box_thresh": 0.5,                # 默认 0.6：货币栏单字 0/8 置信度 0.5~0.6，
                                      # 整行被丢，读不出 0
```

三组参数只在 `ocr_rapid._PARAMS` 里维护一份；`ocr_chain` / `ocr.py` 不重复配置。

### 7.3 降级链与闸门（和第五节的差异）

- 一级 RapidOCR **原图**（不放大）；只有 `score < 0.5` **或** 千分位/位数格式没过，才进二级。
- 二级 PP-OCRv6 tiny **固定 3× 放大**；服务没起就自动跳过（`available()` 为假，不空等 5s 超时）。
- 三级自建字模（4ms），只当最后兜底；它读不出也返回 None。
- 两道闸门：`score ≥ 0.5` + 格式（千分位严格 3 位一组、1~9 位、多位禁前导 0、尾逗号视为截断）。任一不合格 → `None`，调用方走「读不到」分支，**不猜数**。
- 第五节里「两道不过就 scale=3 重试」现在是**跨引擎**重试（换 PP-OCR 管线），不是同引擎换尺度；同一条 ROI 的 1×/3× 一致性由测试 `test_two_scales_agree` 看守。

### 7.4 模型分发与缺模型行为

- 运行时只按 `os.path.dirname(__file__)` 找 `core/assets/rapid/`，不写死任何机器路径；`sjz.spec` 的 `core/assets` datas 会把模型、字模一起打进 `_MEIPASS`。
- 本地模型缺失时，`Det.model_path` / `Rec.model_path` 不传，RapidOCR 用包内同名模型；再缺才会联网下载 —— 冷启动闸门测试（<5s）能拦住「悄悄下载」。
- `rapid.enabled=false` 可整链退回旧 PP-OCR 服务路径；服务与 Rapid 都不可用时 `available()=False`，`read_price` 返回 `None`、`read_text` 返回空串，不抛异常。

### 7.5 验证结果

- 新增 `tests/test_ocr_price.py`：**47 passed**（5.4s）。含 21 条真实 ROI 参数化、打乱第二轮、两尺度、13 组格式闸门、分数闸门、三级链 stub、缺引擎、冷启动。
- 真实截图端到端：21 条 ROI × 2 轮 = **42/42**（正序 / `Random(20261106)` 打乱），p50 13.4ms / 12.5ms，两轮合计 0.8s（冷启动算在第一轮首次调用里）。
- 全量回归（最终）：`pytest tests` **938 passed, 1 skipped**，连跑两遍结果一致（307.62s / 305.13s）。
- 本轮第一次全量跑的 13 failed + 4 errors 全部是测试适配问题、不是产品缺陷，已逐一修掉：
  - `tests/test_packaging.py`：spec 新增了 `collect_data_files("rapidocr")`，测试里的假 `PyInstaller.utils.hooks` 只 stub 了 `collect_submodules` → 补上同名空函数。
  - `tests/test_engine_list_queue.py`：`market.scan` 新增 `upscale` 关键字，测试桩写成 `lambda frame:` → 改成 `lambda frame, **kw:`。
  - `tests/test_ocr_service.py`：`_started_at = 1.0` 依赖「开机超过 6 小时」才成立，本机开机时长不足时会算不出 7 小时 → 改成 `time.monotonic() - 7*3600`，去掉环境依赖。
- OCR 相关子集（改动后）：`pytest tests/test_cards.py tests/test_market.py tests/test_ocr_price.py tests/test_ocr_grade.py` = **142 passed**。
- `py_compile`：`compileall core tools tests desktop.py run.py` 全通过（exit 0）。
- Tesseract：代码与依赖声明中已无引用（仅历史报告里作为被否方案出现）。

### 7.6 遗留三项收尾（2026-11-06 晚，全部真跑）

**① PyInstaller 真实构建 `sjz.spec`**

- 构建：`.venv\Scripts\python.exe -m PyInstaller sjz.spec --noconfirm`，exit 0。
- 第一次构建暴露的真问题：exe 起来后 `--doctor` 报 `OCR 可用 = False`。根因是 `collect_submodules("rapidocr")` 只收 `.py`，`rapidocr/config.yaml` 与 `rapidocr/default_models.yaml` 没进包，`RapidOCR(params=...)` 初始化时读配置直接抛 `FileNotFoundError`（源码跑得通、打包就崩，正是这类「缺数据文件」的典型症状）。
- 修复：`sjz.spec` 加 `datas += collect_data_files("rapidocr")`；同时 `core/ocr.py` 新增 `rapid_error()`，`--doctor` 多打一行 `OCR Rapid 错误`，以后再出这类问题不用猜。
- 重测（打包版自检）：
  - `OCR 后端 = RapidOCR → 二级服务未起（价格二次确认自动跳过） → 字模兜底`
  - `OCR 模型目录 = C:\Users\...\Temp\_MEI*\core\assets\rapid`
  - `OCR Rapid 错误 =（无）`、`OCR 可用 = True`
- 包内核对（`_MEIPASS`）：`PP-OCRv6_det_small.onnx` 9,929,594 B；`PP-OCRv6_rec_small.onnx` 21,234,383 B；`core/assets/digits` **344 个文件**；onnxruntime 相关文件合计 **37.5 MB**。
- 产物体积：`dist/SJZ-Control.exe` **146.9 MB**（140.06 MiB）。
- 首次启动：**5.63s** 到 `窗口已显示`（onefile 解压 + onnxruntime 加载都在里面）；`--doctor` 冷启动 wall 约 5s。二次启动同一 exe 仍是 onefile 解压，不会更快。

**② 整页识别性能收敛**

同机 CPU、1920×1080 真实截图实测：

| 场景 | 优化前 | 优化后 |
| --- | --- | --- |
| `market.scan` upscale=3 | 17.5s | **1765ms** |
| `market.scan` upscale=4 | ~6.8s | **1296ms** |
| `market.scan` upscale=5 | ~5.0s | **1317ms** |
| 整页 1× | — | 609ms |
| 整页 2× | — | 1477ms |
| 只扫列表区 1120×700 | — | 237ms |
| 整页缩到 1280×720 | — | 451ms |
| `Scanner` 静止帧（缓存命中） | — | **16~20ms** |

三个收敛点：

1. `cards.read_card` 默认走 `_whole_rows`：整卡**一次** `ocr.read_words`，按行中心聚词。旧的「4~5 条带 × 3× 放大」是 WinRT/PP-OCR 时代补丁，RapidOCR 原图即可读，逐带推理反而慢 5~7 倍。
2. 补认合并：`_scan_cards` 在价格/品阶缺失时只做**一次** `read_card_bottom(upscale=3)`（卡片底部 38%），替代原来的「5× 整卡 + 两次调用」。
3. 帧缓存：`Scanner.scan` 的 not-found 分支用 480×270 缩略图 + `(absdiff > 20).sum() < 120` 判静止，命中直接返回上一帧结果；列表滚动时缩略图差异超阈值，正常重算。

取舍：极小变化（缩略图 ≤25 像素，如 20×20 白块）仍判静止——这是 16.9ms 的来源，属有意保留。结论：678ms 级别的整页耗时只出现在「首次进列表且卡片定位失败」一次；滚动稳态由缓存接管到 20ms 级，连续读价走 ROI 12~70ms，不会再卡住列表滚动。

**③ 中文品阶/成色基准**

- 数据集 `tests/fixtures/grade_roi/`：29 条真实截图 ROI —— 4 条品阶词（传说/史诗/稀有/普通）、20 条「极品丨成色 X」卡块（A/B/C/S）、5 条负样本（稀有度/成色/所有稀有度/所有成色/典藏）。
- 结果：29/29 × 2 档（1× 原图 + 3×）= **58/58 全对**，**准确率 100%，负样本拒识 5/5，误识 0**。典型：`极品|成色A` score 0.92，品阶词 1.00。
- 误识修复：本轮未发现新增误识；`bricks.parse_wear(strict=True)` 已能拒绝 `M4A1突击步枪-群星`、`SCAR-H突击步枪`、`MP7冲锋枪-电玩高手S2` 这类「名字里带后缀但不含成色」的近似名。
- 测试守卫：`tests/test_ocr_grade.py` 4 条（29×2 参数化 + 基准完整性 4/20/5 + 近似名拒识）。

### 7.7 仍未做

- 二级 PP-OCR 服务的 3× 复核路径没有在打包版上端到端跑过（打包版默认服务未起，该级自动跳过；源码环境下由 `test_ocr_price.py` stub 覆盖）。
- 帧缓存只对「卡片定位失败、退化为整屏 OCR」的分支生效；正常定位路径没有缓存，靠整卡一次推理本身压到 1.3~1.8s。

