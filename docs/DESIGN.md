# rawsqueeze 设计规格 (v1)

> 状态：最终设计（架构评审综合稿）。来源：三位设计者的实测提案。
> A = perceptual-codec（JXL VarDCT/XYB 路线），B = noise-adaptive-quantizer（噪声自适应量化 + JXL 无损路线），C = systems-format（容器、元数据、DNG、CLI、验证）。
> 所有数字都是在 Panasonic DC-S9 样张（6016x4016，12-bit，RW2 v0500）上实测的，除非标注"估计"。
> 实验脚本目录：`/private/tmp/claude-501/-Users-tosaki-raw/b4070295-44c8-498f-be56-8976c4145088/scratchpad/{perceptual-codec,noise-adaptive-quantizer,systems-format}/`

---

## 0. 一页结论

rawsqueeze 是一个**自适应双引擎** RAW 编解码器。它使用全新的 `.rsq` 容器，解码输出标准 DNG（LJ92 压缩，完整 EXIF/MakerNotes）。

| 引擎 | 原理 | 用在哪里 | 依据 |
|---|---|---|---|
| `half3` | 半分辨率 WB+相机矩阵 → 线性 sRGB（float32，3 通道 JXL VarDCT/XYB）+ G1−G2 差分平面（灰度 VarDCT）+ 饱和掩码 | **低噪声**文件（默认：SNR18 ≥ 60，DC-S9 上约 ISO ≤ 450；2026-10 标定，见 Q1） | A：完整 ISO100 文件 d0.2 = 4.89 MB（5.78x），+2/+3EV 画质不低于 sqrt-k2 基线（6.79 MB）；ISO320 裁切上比基线小 3–3.8x |
| `nlq` | 每个 CFA 平面估计噪声模型 var=g·x+s2 → 压扩曲线，量化步长 = f·σ(x)（σ<1 DN 处为恒等）→ 整数平面送 JXL modular **无损**（e3） | **高噪声**文件；`f=0` 时就是**无损模式** | B：ISO4000 f1 = 3.12–3.17x，误差在统计上等价于 +4% 的高斯噪声 RMS；无损 1.34–1.59x |
| `gat4`（实验性） | GAT 归一化后的 4 个 CFA 平面送 JXL VarDCT | 可选，非默认 | B：ISO100 时在相同 ssimulacra2 下比 nlq 小 10–30%，但没有做 AHD/butteraugli 伪影检查 |

- 引擎选择：先估计噪声模型，计算 18% 灰处的 SNR，再按阈值选（第 2.4 节）。也可以用 `--engine` 强制指定。
- 默认预设 `vl`：half3 用 d=0.2，nlq 用 f=1.0。
- 无损：nlq f=0。4 个 CFA 相位平面原值直接做 JXL lossless e3，结果与原马赛克逐位一致（sha256 校验）。

---

## 1. 目标、非目标、实测证据

### 1.1 目标
1. 压缩比第一。默认预设在低 ISO 下应比原始 RW2（本身已有 Panasonic 压缩，约 9.4 bit/px，含预览）小"数倍"。
2. "后期之后视觉无损"：+2..+3 EV 推曝光、提阴影、改白平衡之后看不出差异。评估方法见第 6 节，诚实的阈值说明见第 1.4 节。
3. 速度：24 MP 文件在默认设置下编码 ≤ ~2 s，解码到 DNG ≤ ~1.5 s（8 核 M 系列）。
4. 真无损模式，用于归档，马赛克逐位一致。
5. 尽量复用现有库：rawpy/LibRaw、imagecodecs（libjxl、LJ92）、pidng、zstandard、exiftool、ssimulacra2、butteraugli。
6. 解码器输出标准 DNG，Lightroom、darktable、rawpy 都能打开。
7. 设计面向 LibRaw 能打开的所有 2x2 Bayer 相机，v1 只针对 RW2 做完整验证。

### 1.2 非目标
- 不复原 RW2 原文件的字节（不重新实现 Panasonic 的压缩格式）。"无损"保证三件事：马赛克完全一致；元数据完整（329/329 个 exiftool 标签）；可选保留相机 JPEG（逐位一致）。
- v1 不支持非 Bayer（X-Trans 6x6、CMYG、单色、线性 DNG）。对这类输入，无损模式可以走 fallback，有损模式直接拒绝（见第 8 节）。
- 不输出 DNG 1.7 JXL 压缩：LibRaw 0.22.1 打不开（C 实测）。
- 不做按 tile 混合引擎（容器里预留了位置，v2 再做）。

### 1.3 实测证据汇总

**原始文件构成（C）**：16–24% 的字节是嵌入的预览 JPEG。

| 文件 | ISO | 文件 MB | 头部+预览 MB | Panasonic 原始数据 MB |
|---|---|---|---|---|
| P1060444 | 100 | 28.26 | 6.82 | 21.44 |
| P1037920 | 4000 | 27.07 | 4.90 | 22.17 |
| P1060384 | 320 | 23.59 | 4.13 | 19.46 |
| PANA0003 | 4000 | 26.01 | 4.56 | 21.44 |
| PANA9831 | 100 | 31.77 | 6.56 | 25.21 |

> 所有压缩比都要报两个数：相对整个文件，以及相对原始数据。下表默认是相对整个文件。

**无损（B、C、A）**

| 配置 | P1060444 MB (比) | P1037920 | P1060384 | 编码/解码 s | 来源 |
|---|---|---|---|---|---|
| 4 平面 JXL LL e1 | 19.96 (1.42x) | – | – | 0.06 / 0.17 | B |
| **4 平面 JXL LL e3** | **17.81 (1.59x)** | 20.17 (1.34x) | 15.30 (1.54x) | 0.21 (8 线程) / 0.11–0.28 | B, C |
| 4 平面 e5 / e7 | 17.87 / 17.92 | – | – | 0.61 / 2.6 (8 线程) | C |
| 4 通道堆叠 e3 / e5 | 18.10 / 17.61 | – | – | – / 2.9 | B / probe |
| 整幅马赛克当 2D 图 | 23.80 (+34%) | – | – | – | B |
| 可逆 G/dG/R−G/B−G lifting | 18.65 (+4.7%) | 21.04 | 16.04 | – | B |
| cjxl -e7 -P6 -I100 -g3 | 约 −1.3% | – | – | 30–50x 时间 | B |
| bitspersample=12 / 减 black | 无变化（<0.1%） | – | – | – | A, B, C |
| .rsq 完整容器（e3 + META） | 17.837 (1.58x / 原始数据 1.20x) | 20.200 (1.34x / 1.10x) | PANA9831 22.388 (1.42x / 1.13x) | 0.77 / 0.86（含 DNG+EXIF） | C |

effort 非单调的原因（B）：e3 用的是围绕自校正加权预测器（WP）的固定 MA 树，非常适合传感器噪声。e4 以上会按 group 学习 MA 树，信号开销反而更大。这与 bit 深度信令无关。

**有损：低 ISO（P1060444 ISO100，完整文件，LibRaw AHD 显影，A-E9）**

| 配置 | MB | 比 | 编码 s | ss2 0/+2/+3EV | butteraugli max/p3 (0EV; +3EV) | PSNR +3EV |
|---|---|---|---|---|---|---|
| 基线 sqrt-k2 + JXL LL e5 | 6.79 | 4.17 | 0.46 | 87.30 / 83.51 / 80.61 | 1.51/0.43; 2.71/0.98 | 39.2 |
| **half3 d0.2 e7** | **4.89** | **5.78** | 1.03 | 86.45 / 83.28 / 81.48 | 1.71/0.49; 3.27/0.90 | 36.7 |
| half3 d0.3 e7 | 3.71 | 7.62 | 0.96 | 84.58 / 80.55 / 78.05 | 1.83/0.58; 3.32/1.11 | 35.1 |
| half3 d0.3 e5 | 3.70 | 7.63 | 0.62 | 84.40 / 80.52 / 77.99 | – | 34.9 |
| half3 d0.3 e3 | 3.13 | 9.03 | 0.36 | 83.58 / 78.20 / 74.86 | – | 33.7 |
| half3 d0.45 e7 | 2.77 | 10.22 | 0.96 | 81.94 / 76.45 / 73.19 | 2.09/0.74; 3.89/1.41 | 33.4 |

**有损：nlq（B，完整文件编码，指标取 2 个 1024² 裁切里的较差者，AHD；"ds2" = 2x2 下采样后的 ss2）**

| 文件 | f | MB | 比 | 编码/解码 s | ss2 0/+2/+3EV | 备注 |
|---|---|---|---|---|---|---|
| P1060444 ISO100 | FLOOR（+随机 0/1 DN） | – | – | – | 95.0 / 92.6 / 91.4 | 参照下限 |
| P1060444 | 0.5 | 14.81 | 1.91 | 0.9 / 0.58 | 95.0 / 92.9 / 91.7 | |
| P1060444 | **1.0** | 11.83 | 2.39 | 1.42 / 0.52 | 94.1 / 91.4 / 89.8 | 整图 ds2 +3EV 90.8 |
| P1060444 | 2.0 | 9.10 | 3.10 | 1.41 / 0.50 | 92.5 / 88.9 / 86.6 | |
| P1060444 | 3.0 | 7.69 | 3.68 | 1.39 / 0.49 | 90.3 / 85.1 / 81.9 | |
| P1060384 ISO320 | 1.0 / 2.0 / 3.0 | 8.47 / 5.95 / 4.76 | 2.78 / 3.96 / 4.96 | ~0.7 / 0.5 | +3EV 86.5 / 80.3 / 72.7 | FLOOR +3EV 89.5 |
| P1037920 ISO4000 | FLOOR | – | – | – | 85.8 / 77.9 / 73.9 | 只要扰动了颗粒实现就会扣分 |
| P1037920 | 0.5 / **1.0** / 2.0 | 11.45 / 8.53 / 6.01 | 2.36 / 3.17 / 4.51 | ~0.8 / 0.5 | +3EV 61.1 / 41.3 / 4.1 | 噪声 std 比 1.007 / 1.023 / 1.089 |
| PANA0003 ISO4000 | 1.0 / 2.0 / 3.0 | 8.33 / 5.86 / 4.65 | 3.12 / 4.44 / 5.59 | 1.42 / 0.51 | +3EV 35.4 / −8.3 / −40.7 | 噪声 RMS +2.4–4.8% / +10–17% / +23–35% |
| PANA9831 ISO100（g 高估 2.3x） | 1.0 / 2.0 / 3.0 | 14.72 / 11.82 / 10.22 | 2.16 / 2.69 / 3.11 | 1.46 / 0.56 | +3EV 88.0 / 84.0 / 78.9 | |

**有损：高 ISO 下 half3 失败（A-E9，P1037920 ISO4000，完整文件，AHD）**

| 配置 | MB | 比 | ss2 0/+2/+3EV | butteraugli max (0EV; +3EV) |
|---|---|---|---|---|
| 基线 sqrt-k2 + LL | 9.66 | 2.80 | 83.37 / 69.11 / 62.59 | 2.26; 3.52 |
| half3 d0.15 | 10.14 | 2.67 | 79.29 / 61.91 / 54.37 | **11.69; 22.23** |
| half3 d0.3 | 6.22 | 4.35 | 73.61 / 48.23 / 37.39 | 12.15; 22.08 |

**有损：gat4 = VarDCT-on-GAT（B，同 nlq 的度量方法）**

| 文件 | d | MB | 比 | ss2 0/+2/+3EV |
|---|---|---|---|---|
| P1060444 | 0.1 / 0.2 / 0.4 | 10.84 / 7.73 / 5.10 | 2.61 / 3.66 / 5.54 | 94.1/91.7/90.5; 92.5/89.2/87.3; 88.5/83.4/80.3 |
| P1037920 | 0.1 / 0.4 | 15.59 / 8.64 | 1.74 / 3.13 | 86.0/79.0/75.1; 74.4/58.1/49.5 |

**裁切实验（A，2048² 裁切，自写 numpy bilinear 显影；体积按全幅外推，带 \*）**：ISO320 P1060384 上，half3 d0.2 2.41\* MB（9.8x）+3EV 84.55，基线 k3 5.94\* MB 85.64，即 half3 小 3–3.8x。ISO100 PANA9831 上 half3 d0.2 与基线 k1.5 相比优势只有 1.24x。ISO100 高光角落只有 1.29x。half3 d0.1 在 ISO100 中心裁切上 +3EV 90.22（大约是 d0.2 体积的 1.5x）。注意：bilinear 裁切比 AHD 全幅更乐观，同一文件上分别是 1.84x 和 1.39x。

### 1.4 关于"视觉无损"阈值的诚实说明（裁定）
1. 在高 ISO 下，全参考指标给不出绝对阈值。B 的 FLOOR 对照显示：在 ISO4000 上，仅仅加随机 0/1 DN，+3EV 的 ss2 就只剩 73.9。这些指标度量的是"颗粒实现变了"，而这在视觉上不可见。所以对 nlq 采用**噪声相对准则**：附加噪声 RMS = sqrt(1+f²/12)−1。f=1 是 +4%，f=2 是 +15%，f=3 是 +32%。另加偏置与噪声 std 比的检测（第 6 节）。
2. 在低 ISO 下，half3 d0.2 的完整文件 AHD 结果是 +3EV ss2 81.5，butteraugli 3-norm 0.90（<1.0），与 sqrt-k2 基线相当。按"≥80 为极高质量"属于近视觉无损；按严格的"≥85"标准，只有 `high` 预设（d0.1）能做到。用户把压缩比放在第一位，所以默认取 d0.2，并在 `verify` 里把这个事实明确报出来。见开放问题 Q4。
3. 结论：每个预设的验收标准按引擎分别定义（第 6.4 节），不追求跨引擎的同一个 ss2 数值。

---

## 2. 编解码流水线

### 2.0 记号
- `m`：完整传感器马赛克 `raw_image`（H×W，uint16，含 margins）。DC-S9 上是 4016×6016，visible == full。
- `pat`：`raw_pattern`（2×2，取值是颜色索引），`desc`：`color_desc`（如 `b'RGBG'`）。
- 位置 `p ∈ {(0,0),(0,1),(1,0),(1,1)}`，平面 `P_p = m[dy::2, dx::2]`，每个平面 (H/2)×(W/2)。
- 每个位置的黑电平 `blk_p = black_level_per_channel[pat[dy,dx]]`（数组按**颜色索引**组织，必须经 pat 映射到位置）。白电平 `wl = white_level`（DC-S9 是 4079；原始值最高到 4095）。
- 颜色角色：`desc[pat[p]] == 'R'` 的位置为 R，`'B'` 为 B，两个 `'G'` 的位置按光栅顺序分别记作 G1、G2。若 desc 中不恰好是 1R+2G+1B，就不是 RGGB 类 Bayer，half3/gat4 不可用（见第 8 节）。

### 2.1 公共前处理（编码器）
1. `rawpy.imread(src)`，取 `m = raw_image.copy()`（完整传感器）、`pat`、`desc`、`black_level_per_channel`、`white_level`、`camera_whitebalance`、`daylight_whitebalance`、`rgb_xyz_matrix[:3]`、`sizes`（raw_height/width、top/left_margin、crop_*、flip）。
2. 用一次 exiftool 调用取 `-j -n -ISO -Make -Model -RawDataOffset` 等字段（META 本来就要调用 exiftool，见第 3 节）。
3. 尺寸：若 H 或 W 为奇数，先边缘复制补到偶数，HEAD 里记录原始尺寸，解码后裁回。
4. 计算 `sha256(m.tobytes())` 写入 HEAD。
5. 噪声模型（第 2.3 节）。无损模式跳过；`--engine half3` 时只为记录和 DNG NoiseProfile 计算，可用 `--no-noise` 关闭。
6. 选择引擎（第 2.4 节）。

### 2.2 引擎 nlq（噪声自适应量化 + JXL 无损）。f=0 时即为无损模式

**编码（对每个位置 p 独立做）**
1. `x = P_p.astype(int32) − blk_p`。有损时先把 `m` 中 ≥ wl 的值钳到 wl（DC-S9 的 4080..4095 变成 4079），即 `x ≤ X = wl − blk_p`。
2. 参数 `(g_p, s2_p)` 来自第 2.3 节，单位是 DN 和 DN²；所有平面共用同一个 f（B：对 R/B 加粗量化没有率失真收益）。
3. 压扩曲线（B，已验证）：
   - `x0 = max((1/f² − s2)/g, 0)`，`c0 = sqrt(g·x0 + s2)`
   - `y(x) = x`，当 `x < x0`（恒等区，即无损区）
   - `y(x) = x0 + (2/(g·f))·(sqrt(g·x + s2) − c0)`，当 `x ≥ x0`
   - 斜率 dy/dx = 1/(f·σ(x))，所以每个整数码的步长 = f·σ(x) DN；在 f·σ < 1 DN 处退化为恒等（不过采样阴影）。B 实测：纯 GAT 在 f0.25 时比无损还大。
4. 负值（低于黑电平）：恒等区向负方向延伸。`o_p = max(0, −min(x))`，`q = round(y(x)) + o_p`。DC-S9 上原始值最小就是 black，所以 `o=0`。这样对会输出低于黑电平数据的相机也是可逆的（B 的风险项）。
5. 饱和码：`q_sat = round(y(X − 1)) + o_p + 1`。原始值 ≥ wl 的像素一律写 `q_sat`，其余像素的 q 不会达到 `q_sat`。解码 `LUT[q_sat] = wl`，保证裁切高光不会因 LUT 舍入落到 wl 以下（否则推曝光后会出现洋红色高光，C 的风险项）。
6. 取整：确定性 `np.rint`。**不加 dither**（B：dither 体积 +2.4–12%，等体积比较时 ss2 更差）；**不用 deadzone**。
7. dtype：`qmax = q_sat < 256` 时用 uint8，否则 uint16。不要对 uint16 输入传 `bitspersample<8`，会报 `JXL_ENC_ERR_GENERIC`（B）。`bitspersample=None`。
8. 重建 LUT（float64 计算，结果存为 uint16）：
   - `mid`：`LUT[k] = clip(rint(curve_inv(k − o_p) + blk_p), 0, wl)`
   - `centroid`：`LUT[k] = rint(mean(P_p[q==k]))`，用 `np.bincount` 算；空 bin 回退到 mid；结果再钳到该 bin 的 [下沿, 上沿] 内以保持单调。
   - 默认：`f < 2` 用 mid，`f ≥ 2` 用 centroid。B 的数据：f≤2 时 mid 的偏置 <0.5 个 sRGB-8bit 级；f3 在 ISO4000 近黑处达 −1.1 级，建议用 centroid。
   - `LUT[q_sat] = wl`（无条件覆盖）。
   - `curve_inv(y)`：`y < x0` 时 `x = y`；否则 `t = (y − x0)·g·f/2 + c0`，`x = (t² − s2)/g`。
9. 熵编码：`imagecodecs.jpegxl_encode(q, lossless=True, effort=3, numthreads=nt)`，4 个平面放进 ThreadPoolExecutor(4)，每个 `nt = max(1, threads//4)`（C：与单流 8 线程一样是 0.21 s，而且解码也能并行）。
   - `--effort ≥ 5` 时改用 `layout=stack4`：一个 H/2×W/2×4 的 4 通道图，effort=7（B：q 平面小 3.9–4.2%，编码慢约 8x）。无损 stack4 e5 = 17.61 MB（probe，比 e3 小 1.1%）。
10. 无损（f=0）：不压扩、不钳位、不建 LUT，直接编码原始 `P_p`（uint16，**不减黑电平**，实测无收益）。4080..4095 的值原样保留。

**解码**
1. `q = jpegxl_decode(PLNk)`，reshape 成 (H/2, W/2)；stack4 时取 `[..., k]`。
2. 有损：`P̂_p = LUT_p[q]`。无损：`P̂_p = q`。
3. 交织回马赛克，必要时裁掉补边。无损要比对 sha256，不一致就报错退出；有损比对编码端记录的 `recon_sha256`，仅作提示（LUT 精确，所以 nlq 应当逐位一致）。

**性质（B 实测）**：量化误差 ≈ 独立高斯噪声，std = f/√12·σ(x)。与合成噪声对照在 0.1 ss2 以内，无条带、无偏置。码率上限约为 2 + log2(1/f) bit/px 加上纹理部分。

### 2.3 噪声模型估计（estimate_noise2，B）
对每个平面 P_p，实现见附录 A.3：
1. `x = P_p − blk_p`（float32）。`r = (x − 四个同色邻点均值) / sqrt(1.25)`。
2. 切 8×8 块，算块均值；块 σ = 1.4826·MAD(r)。丢掉 `min ≤ 0` 或 `max ≥ 0.95·X` 的块。
3. 按块均值分 48 个等人口 bin（每个 bin 至少 50 块）。每个 bin 的 `var = P5(σ)² / cf`，其中 `cf` 是在纯 N(0,1) 上用相同统计量模拟得到的 P5(σ)²（固定种子 0，1024² 采样，结果缓存）。**这个偏置校正必不可少**：不做的话 g 会低 25–35%。
4. 加权最小二乘拟合 `var = g·mean + s2`。初始化只用 `var/(mean+20)` 落在最低 40% 的 bin；之后迭代 4 次，每次剔除 `var > 1.3·pred` 的 bin。最后钳到 `g ≥ 1e-4`、`s2 ≥ 0.25`。
5. ISO 先验上限（noise_model=`auto+iso_cap`，有相机表时为默认）：`g_used = min(g_est, k_cam[p]·ISO)`。DC-S9：R/G1/G2 是 6.5e-4/ISO，第 4 个位置（B）是 4e-4/ISO。它专门处理 PANA9831 这类缺少平坦中间调、g 被高估约 2.3x 的场景。相机表放在 `rawsqueeze/noise_table.py`，按 `(make, model)` 索引；不在表里的相机用纯 `auto`。
6. 4 个平面放线程池并行，约 0.65 s 单线程 → 约 0.2 s。
7. 解码器不需要 (g, s2)，因为 LUT 已存储。参数仍写入 HEAD，用于 DNG NoiseProfile 和 verify。

### 2.4 引擎选择（`--engine auto`，默认）
- 取 G1/G2 的 `g_used` 均值 `ĝ` 和 `ŝ2`；令 `x18 = 0.18·X`，`SNR18 = x18 / sqrt(ĝ·x18 + ŝ2)`。
- 若 `SNR18 ≥ T_snr`（默认 **60**，原为 40，已按 Q1 标定）且 CFA 为 RGGB 类 Bayer，选 half3，否则选 nlq。
- DC-S9 估算：ISO100 ≈ 107，ISO320 ≈ 73，ISO800 ≈ 38，ISO4000 ≈ 17。阈值 40 ≈ ISO 700。half3 实测良好的范围是 ISO ≤ 320，实测失败的是 ISO4000；中间 800–3200 **没有数据**，所以阈值偏保守（宁可落到 nlq：画质安全，只是压缩比低一些）。见开放问题 Q1。
- **Q1 已解决（2026-10）**：用 13 张 DC-S9 样张（ISO 100–51200）标定（表格见 docs/STATUS.md「阈值标定」）。SNR18 ≤ 40（ISO ≥ 800）时 half3 在等质量下不比 nlq 小，且 +3EV 颗粒被压平（噪声比 0.85–0.93）；SNR18 ≥ 70 时 half3 只有 nlq 体积的 0.29–0.51。40–70 之间唯一的样张（ISO640）受色域问题污染，因此取 **60** 留余量。色域问题（XYB 钳掉负的 opsin 混合值）另由 half3 的矩阵混合门控修复（R7）。
- 为什么不用 G1−G2 的 MAD：A 实测它分不开纹理和噪声（PANA9831 ISO100 读数 0.89，ISO4000 是 0.85–0.90）。估计器高估 g 时会偏向 nlq，方向是安全的。
- 预设和 `--quality` 的映射按引擎分别给（第 4 节）。`-q` 的语义随引擎变化：half3/gat4 是 d，nlq 是 f。

### 2.5 引擎 half3（A）
前提：RGGB 类 Bayer；`camera_whitebalance` 有效（若前三个值中有 0 或无效，就用 `daylight_whitebalance`，再不行用 [1,1,1]）；`rgb_xyz_matrix` 非零（全零时 `M = I`，即仅 WB；A 实测率失真略差，但可用）。

**编码**
1. `m ← min(m, wl)`。饱和掩码 `S = (m_orig ≥ wl)`，在马赛克坐标下（H×W）。
2. 归一化：`n_p = clip((P_p − blk_p)/(wl − blk_p), 0, 1)`，float32。
3. 白平衡：`wb = cwb[:3] / cwb[1]`（float64）。DC-S9：[534,256,431] → [2.0859, 1, 1.6836]。
4. 矩阵（dcraw 约定）：`c = rgb_xyz_matrix[:3] @ XYZ_from_sRGB_D65`，行归一（`c /= c.sum(1, keepdims=True)`），`M = inv(c)`（srgb_from_cam）。M 和 Minv 都以 float64 存进 HEAD，**解码器绝不从 LibRaw 重新计算**。
5. `rgb = stack([R·wbR, (G1+G2)/2, B·wbB], −1) @ M.T` → float32，**不裁剪**。超过 1 的值（最高约 3.8）会保留；JXL 会把低于约 −0.0038 的线性值钳掉（A-E8：真实裁切上的暗部偏置 ≤ 3e-4 归一化值，约 1 DN）。
6. `b_rgb = jpegxl_encode(rgb, distance=d, effort=5, numthreads=N)`。float 输入默认按线性 sRGB 信令，所以 XYB 看到的是真实线性场景色。effort 5 的率失真与 7/9 相同，快约 35%；effort 3 体积更小，但同一 d 下率失真更差。
7. `D = (G1 − G2) + 0.5`（float32），`b_D = jpegxl_encode(D, distance=dD, effort=5)`，默认 `dD = d`（A：两路的边际率失真相等）。D 占码流的 26–30%，不能丢。
8. `b_sat = zstd19(np.packbits(S.ravel()))`（78 B 到 8 KB）。**不要**用 JXL lossless 存掩码：即使掩码几乎为空也有约 5 KB 开销。
9. rgb 和 D 两路编码在两个线程里并行，numthreads 按比例 3:1 分配。
10. **最大误差保护（H3FX，2026-10 新增，默认开）**：编码后在进程内把 H3RG/H3DG 解码一次（与解码器同一个 `_reconstruct`），对未饱和像素，凡 `|err| > max(t·(wl − blk_p), k·σ_p(x))` 的像素把原值 `min(m_orig, wl)` 精确存进稀疏 chunk `H3FX`。
    - 默认 `k = 8`、`t = 0.2·d`（d0.1/0.2/0.3 → t 0.02/0.04/0.06 = 79/158/237 DN，12-bit）。σ 取自噪声模型 `σ² = g·(x − blk) + s2`；没有噪声模型时只用绝对阈值。
    - 修补像素上限为全部像素的 0.2%（至少 1024 个），超出时保留 `|err|/阈值` 最大的那些。
    - 它主要抓 D 平面被 libjxl 钳位（D < 约 −0.0038，即 `G1 − G2 < −0.504`，单向误差可达数百 DN）以及部分饱和 quad 的离群点。DC-S9 vl 实测：修补 38–5101 像素，占文件 0.015–0.29%，max |err| 308–799 → 158 DN，编码多约 0.2–0.35 s；4 tile 的 ss2/ba p3 基本不变（它消除局部离群点，不提高整体画质）。
    - 进程内解码顺便得到精确重建：开启保护时总会写 `recon_sha256`，`--verify` 不再额外解码。
    - 开关：API `fixup` / `fixup_k` / `fixup_t`（`EncodeParams.extra`），CLI `--no-fixup` / `--fixup-k` / `--fixup-t`。gat4 使用同一个保护（很少触发）。
    - HEAD 记录 `codec.half3.fixup = {k, t, t_dn, noise, n, n_over, capped, err_max_before, err_max_after}`。

**解码**
1. `cam = jpegxl_decode(b_rgb).astype(float64) @ Minv.T`；`D = jpegxl_decode(b_D).astype(float64) − 0.5`。解码后要确认 dtype 是 float32；若库返回整数，按位深归一化（实现时用断言检查）。
2. `R = cam0/wbR`，`B = cam2/wbB`，`G1 = cam1 + D/2`，`G2 = cam1 − D/2`。
3. 反归一化：`v = clip(rint(n·(wl − blk_p) + blk_p), blk_p, wl)`，转 uint16，交织回马赛克。
4. 若有 `H3FX`：把其中的原值写回对应像素（顺序：clip → H3FX → SATM）。没有 H3FX 的旧文件照常解码；HEAD 记录 `fixup.n > 0` 而 chunk 缺失时报错。H3FX 存的是原值而不是残差，所以浮点解码的平台差异不影响修补结果。
5. `m̂[S] = wl`（掩码精确恢复裁切）。
6. 确定性：不同 libjxl 版本或平台的浮点解码可能有微小差异。HEAD 里的 `recon_sha256` 只作提示，`codec.libjxl_version` 必须记录。

**已知局限**：部分饱和的 2×2 quad 边缘误差最高约 900 DN（A-E8，像素占比 0.01–0.1%，集中在饱和点 6 px 以内）。离开高光区域后最大约 510–550 DN，p99.99 为 95 DN（整幅实测为 199 DN，见 STATUS 偏差 4）。开启 H3FX 后（默认）低 ISO 文件的 max |err| 被限制在 `max(t·(wl−blk), 8σ)`，d0.2 时为 158 DN；强制 half3 的高 ISO 文件上限随噪声增大（8σ 大）。

### 2.6 引擎 gat4（B，实验性，P2 优先级）
- 对每个平面：`y = (2/g)·sqrt(max(g·x + 3/8·g² + s2, 0))`（单位方差域），`a = rint(y·K)`，uint16，**固定 K = 100**（每个 σ 100 个码值，使 d 的含义与 ISO 无关；B 测试时用的是 `K = 65535/ymax`）。然后 `jpegxl_encode(a, distance=d, effort=5)`。
- 解码：`ŷ = a/K`，`x̂ = ((g·ŷ/2)² − 3/8·g² − s2)/g`，加回 `blk_p` 并钳位；饱和像素用与 half3 相同的 SATM 掩码。
- 需要在 HEAD 中存 g、s2、K（float64）。解码依赖浮点运算，确定性与 half3 同级。
- 只在 `--engine gat4` 时启用。实现后必须先跑完第 6 节 AHD + butteraugli 全文件验证，才能考虑把它放进 auto。

### 2.7 黑电平、白电平、CFA、margins、裁切的处理汇总

| 项目 | 规则 |
|---|---|
| 黑电平 | 按位置取 `blk_p`（颜色索引经 pat 映射）。无损不减；nlq/gat4 平面内减去；half3 归一化时减去。DNG 写 `BlackLevelRepeatDim=[2,2]`、`BlackLevel=[blk_p...]`（SHORT）。LibRaw 的 cblack 二维图案（cblack[6:]）v1 不支持：如果 rawpy 报告的黑电平表不是 4 个值都可用，就拒绝有损编码，只做无损（见 R8） |
| 白电平 | `wl = white_level`。无损保留 > wl 的值；有损先钳到 wl，再由饱和码或掩码精确恢复 wl。DNG `WhiteLevel = wl` |
| 位深 | 无损的 JXL 输入为 uint16，`bitspersample=None`（12 无收益）；nlq 的 q 按 qmax 用 uint8 或 uint16。DNG 的 `BitsPerSample = max(12, ceil(log2(max(m̂)+1)))`，LJ92 的 `bitspersample` 与之相同 |
| CFA | 只支持 2×2，`pat` 和 `desc` 原样写入 HEAD；DNG `CFAPattern` = 位置上的 R/G/B 码 [0,1,1,2] 等 |
| margins | 编码完整 `raw_image`（含 margins）。DC-S9 上 top/left_margin=0，8 px 边框是真实图像数据。有遮光区的相机：HEAD 记录 margins，DNG 写 `ActiveArea=[top,left,top+h,left+w]`；有损模式下遮光区随平面一起编码（量化误差可以接受，因为黑电平已存为数值） |
| 裁切 | `crop_ltwh = sizes.crop_*`（DC-S9：8,8,6000,4000）写入 HEAD，DNG 写 `DefaultCropOrigin`、`DefaultCropSize` |
| 方向 | `sizes.flip` → DNG Orientation：{0:1, 3:3, 5:8, 6:6} |
| 奇数尺寸 | 边缘复制补到偶数，解码后裁回 |

---

## 3. .rsq 容器格式

### 3.1 字节布局（全部小端）
```
off   size  field
0     8     magic = 89 52 53 51 0D 0A 1A 0A   ("\x89RSQ\r\n\x1a\n", PNG 风格，能发现文本模式损坏)
8     2     u16 major = 1
10    2     u16 minor = 0
12    ...   chunk*        (第一个必须是 HEAD；INDX 必须是最后一个 chunk)
EOF-12 8    u64 offset_of_INDX_chunk_header
EOF-4  4    b"RSQE"       (尾标；缺失 = 截断)
```
单个 chunk：
```
+0    4     fourcc (ASCII)
+4    4     u32 flags: bit0 ZSTD(payload 是一个 zstd frame) ; bit1 CRITICAL ; 其余保留=0
+8    8     u64 N = 存储的 payload 长度
+16   N     payload
+16+N 4     u32 crc32 = zlib.crc32(payload, zlib.crc32(header16))
```
读取规则：magic 不对 → 报错；major ≠ 1 → 报错；minor 更高 → 允许；任一 CRC 失败 → 报错（`--force` 时可跳过有损载荷的校验，仅限调试）；未知 CRITICAL chunk → 报错；未知非关键 chunk → 跳过；缺少尾标 → 报 "truncated"。INDX 缺失或损坏时，从偏移 12 起顺序扫描重建索引。

### 3.2 chunk 类型

| fourcc | crit | zstd | 内容 | 出现条件 |
|---|---|---|---|---|
| `HEAD` | Y | Y | UTF-8 JSON（见第 3.3 节），约 600 B | 总是 |
| `LUTS` | Y | Y | 4 段：`u32 n` + `n×u16` LUT（位置顺序 (0,0),(0,1),(1,0),(1,1)） | nlq 有损 |
| `PLN0`..`PLN3` | Y | N | JXL 裸 codestream（`usecontainer` 关），分别对应 4 个位置 | nlq 或无损，layout=planes |
| `PLNS` | Y | N | 4 通道 JXL codestream | layout=stack4 |
| `H3RG` | Y | N | half3 rgb 的 JXL VarDCT codestream | half3 |
| `H3DG` | Y | N | half3 D 平面的 JXL codestream | half3 |
| `SATM` | Y | Y | `np.packbits(S.ravel(), bitorder='big')`，按行优先，H×W bit（ZSTD 标志位表示 zstd-19） | half3、gat4 |
| `H3FX` | Y | Y | 最大误差修补（2.5 节第 10 步）：头 `<BBHIII>`（version=1、flags=0、reserved=0、n、H、W），然后 n 个排序后行优先像素索引的 LEB128 差分（第一个差分 = 第一个索引），最后 n 个原始 uint16 值，先低字节段、后高字节段 | half3、gat4，且有像素需要修补时（默认开，`--no-fixup` 关） |
| `G4P0`..`G4P3` | Y | N | gat4 平面的 JXL codestream | gat4 |
| `META` | N | Y | 元数据骨架（第 3.4 节），zstd-19 | 默认（`--no-meta` 时省略） |
| `PRV0`/`PRV1` | N | N | 相机 JPEG 的 JXL lossless-JPEG 转码（PRV0=JpgFromRaw，PRV1=JpgFromRaw2） | `--keep-preview small/full` |
| `TILE` | Y | – | 保留：v2 按 tile 混合引擎用 | v1 不写 |
| `RESD` | Y | – | 保留：有损 → 无损的残差层 | v1 不写 |
| `INDX` | N | N | JSON `[[fourcc, offset, length], ...]` | 总是 |

### 3.3 HEAD JSON（float 用 Python `json` 的 repr，float64 可精确往返）
```json
{
 "format": "rsq", "version": [1,0], "encoder": "rawsqueeze 0.1.0",
 "mode": "lossy|lossless", "preset": "vl", "engine": "half3|nlq|gat4",
 "codec": {"libjxl_version": "0.12.0", "imagecodecs": "2026.8.x",
           "layout": "planes|stack4", "effort": 3,
           "nlq": {"f": 1.0, "recon": "mid|centroid", "planes": [{"g":0.062,"s2":4.1,"offset":0,"q_sat":431,"dtype":"uint16"}, "..."]},
           "half3": {"d": 0.2, "dD": 0.2, "wb": [2.0859375,1.0,1.68359375], "M": [[...],[...],[...]], "Minv": [[...]], "matrix": true,
                     "matrix_blend": 0.0, "fixup": {"k": 8.0, "t": 0.04, "n": 5101, "err_max_before": 590, "err_max_after": 158, "...": "..."}},
           "gat4": {"d": 0.2, "K": 100.0, "planes": [{"g":..,"s2":..}]}},
 "noise": {"model": "auto+iso_cap", "planes": [{"g_est":..,"g_used":..,"s2":..}], "snr18": 107.3, "iso": 100},
 "source": {"name": "P1060444.RW2", "size": 28262912, "sha256": "...", "make": "Panasonic", "model": "DC-S9",
            "raw_data_offset": 6823936, "libraw_version": "0.22.1"},
 "mosaic": {"height": 4016, "width": 6016, "orig_height": 4016, "orig_width": 6016, "dtype": "uint16",
            "bits": 12, "sha256": "...", "recon_sha256": "..."},
 "cfa": {"pattern": [[0,1],[3,2]], "color_desc": "RGBG", "dng_cfa": [0,1,1,2]},
 "levels": {"black_per_position": [128,128,128,128], "white": 4079},
 "color": {"camera_wb": [534,256,431,0], "daylight_wb": [...], "xyz_to_cam": [[...],[...],[...]], "illuminant": 21},
 "geometry": {"margins": [0,0], "crop_ltwh": [8,8,6000,4000], "flip": 0},
 "meta": {"strategy": "skeleton|exif-only|none", "previews": []}
}
```

### 3.4 元数据骨架（META，C）
- **骨架策略**（RW2 已验证，其它 TIFF 类 raw 同理）：骨架 = 原文件中原始像素数据区之前的部分 `data[0:raw_data_offset]`，其中嵌入的预览 JPEG（JpgFromRaw、JpgFromRaw2）**只把 SOS 之后的熵编码扫描数据清零**，保留所有 JPEG marker segment。然后 zstd-19，得到 22.7–32.4 KB，329/329 个标签全保留，耗时约 0.3 s（含 exiftool）。
- **关键陷阱**：RW2 的完整 EXIF（72 个 ExifIFD 标签）、Panasonic MakerNotes（119 个）和 GPS（13 个）都在嵌入 JpgFromRaw 的 APP1 里。把整个 JPEG 清零会丢掉它们（C 实测：8 KB 的骨架里 Panasonic 标签和 GPS 标签都是 0 个）。
- raw 数据定位：RW2 用 `RawDataOffset`；其它 TIFF 类 raw（NEF/CR2/ARW/ORF/PEF/DNG）用 raw IFD 的 StripOffsets/StripByteCounts 或 TileOffsets，把这段区间清零（不截断），零字节在 zstd 下几乎不占空间。
- **兜底**（`strategy="exif-only"`）：定位失败时存 `exiftool -j -b` 的 JSON（14.5–17.5 KB zstd）加上 `exiftool -o x.exif`（36–40 KB）。这样只能得到部分元数据，并在 info 里给出警告。
- 预览：默认丢弃（每个文件省 4.3–6.8 MB）。`--keep-preview small` 用 `cjxl --lossless_jpeg=1 -e 7` 存 JpgFromRaw（939,239 → 739,841 B）；`full` 再加 JpgFromRaw2（5,878,272 → 4,689,592 B）。`djxl` 能还原逐位一致的 JPEG。解码时用 `--extract-preview` 导出。

### 3.5 DNG 重建（C）
1. 解码得到马赛克 `m̂`（H×W uint16）。
2. `build_tags`（附录 A.4）：ImageWidth/Length、TileWidth/Length=256、PhotometricInterpretation=32803（CFA）、SamplesPerPixel=1、BitsPerSample（见第 2.7 节）、CFARepeatPatternDim=[2,2]、CFAPattern、CFAPlaneColor=[0,1,2]、CFALayout=1、BlackLevelRepeatDim、BlackLevel、WhiteLevel、ColorMatrix1（rgb_xyz_matrix，分母 10000 的 SRATIONAL）、CalibrationIlluminant1=21（D65）、AsShotNeutral（用相机 WB 整数构成的精确有理数 [wbG/wbR, 1, wbG/wbB]）、BaselineExposure=0、ActiveArea、DefaultCropOrigin/Size、Orientation、Make/Model/UniqueCameraModel、DNGVersion 1.4 / DNGBackwardVersion 1.1、Software=`rawsqueeze <ver> (<engine> <param>)`。
   - 可选（默认开）：`NoiseProfile`，取 `S_c = g_c/(wl−blk)`、`O_c = s2_c/(wl−blk)²`，有损时把 f²/12 的附加方差折算进去。
   - `CFAPattern` 和 `BlackLevel` 的 2×2 重复图案以 **ActiveArea 原点**为相位基准（DNG 规范）；HEAD 里的 `dng_cfa` / `black_per_position` 以马赛克 (0,0) 为基准，所以 margins 为奇数时按 `out[2i+j] = v[2·((i+top)%2) + (j+left)%2]` 旋转（`dng.rotate_cfa_phase`，2026-10 修复；margins 为偶数时不变，DC-S9 为 0,0）。
   - **镜头畸变 `OpcodeList3`（2026-10 新增，默认开）**：Panasonic 的机内畸变校正参数（MakerNote `DistortionInfo` 0x0119，16 个 int16，word 0/1/14/15 为校验和；word7 & 0xF == 1 表示开启）转换为一个 `WarpRectilinear` opcode（tag 51022，UNDEFINED，大端，单平面，version 1.3.0.0，flags = 1 Optional，88 字节）。
     - Panasonic 模型（对 13 张样张用相机 JPEG 实测辨识）：`r_out = S·(r + a·r³ + b·r⁵ + c·r⁷)`，`S = 1/(1 + w5/32768)`，`a = w8/32768`，`b = w4/32768`，`c = w11/32768`；半径除以 `N = w12` 像素（DC-S9 为 3606，即 6000×4000 输出裁切的半对角线）；中心为 DefaultCrop 中心。
     - 转换：用 Newton 法求 Panasonic 多项式的逆（参数不单调时拒绝），再在整幅 stage-3 图像上按半径加权最小二乘拟合 `kr0 + kr1·r² + kr2·r⁴ + kr3·r⁶`；中心、归一化按 DNG SDK `dng_lens_correction.cpp` 的约定（NR = 中心到四角的最大距离）。拟合误差 0.004–0.37 px，超过 1.0 px（`LENS_FIT_MAX_ERR_PX`）则不写。
     - 验证：13 张样张以相机 JpgFromRaw2 为几何真值，校正后角落残差中位数 0.14–0.54 px（不校正 6.6–165 px）；Apple ImageIO/`sips` 渲染独立确认 3 张。LibRaw/rawpy 忽略该 opcode（`raw_image` 不变）。
     - 开关：`write_dng(lens_opcode=...)` / `decode_file(lens_opcode=...)`、CLI `decode --no-lens-opcode`、环境变量 `RAWSQUEEZE_DNG_LENS_OPCODE=0/1`。原始参数始终保留在 XMP `rawsqueeze:PanasonicDistortionInfo`。不写 opcode 时（关闭、校验和不符、参数退化、拟合误差过大）给出警告；相机本身关闭校正时不警告。横向色差（0x011B）未转换。
3. 像素：`LJ92DNG`（pidng 子类，附录 A.5），256×256 tile，每个 tile reshape 为 (th/2, 2·tw) 后交给 `imagecodecs.ljpeg_encode`，让上方预测器看到同色像素（C：31.6 → 21.6 MB）。边缘 tile 补零（LibRaw 验证逐位一致）。tile 编码放线程池并行（目前单线程 0.33 s）。
   - `--dng-compression none12|none16`：36.2 MB / 0.05 s，48.3 MB / 0.03 s。
4. EXIF 搬运（单次 exiftool，约 0.35 s）：先把 META 写成临时文件 `skel.rw2`，再运行：
   ```
   exiftool -b -JpgFromRaw skel.rw2 > jfr.jpg
   exiftool -q -q -overwrite_original \
     -tagsFromFile jfr.jpg -exif:all -makernotes -gps:all --IFD0:all --IFD1:all --ThumbnailImage \
     -tagsFromFile skel.rw2 -IFD0:Make -IFD0:Model -IFD0:Orientation -xmp:all  out.dng
   ```
   结果：119 个 Panasonic 标签、13 个 GPS、41 个 ExifIFD；`exiftool -validate` 只有 2 条从相机数据继承来的次要警告。批处理时每个 worker 保持一个 `exiftool -stay_open True -@ -` 进程。若 meta.strategy 不是 skeleton，就跳过第一步，直接从 `.exif` 文件拷贝。
5. 写入是原子的：先写 tmp 再 rename。
6. pidng 陷阱（C）：它会写一个覆盖全图的 tile，必须设置 TileWidth/Length 或使用子类；它会硬编码 Software 和 DNGVersion，重复添加同一个标签会导致重复；`compress=True` 会 import 一个不存在的 `ljpegCompress` 模块；Rational 必须写成 [num, den] 对；BlackLevel 是 SHORT；`convert(filename)` 会自动追加 `.dng`；`convert()` 不传文件名时返回 bytearray；pidng 没有 EXIF 子 IFD 支持，EXIF 交给 exiftool。

---

## 4. 参数与预设

### 4.1 预设（`--preset`，默认 `vl`）
注意命名：`high` 表示**更高保真**（比默认更保守），不是更高压缩。

| 预设 | half3（低噪声） | nlq（高噪声） | 预期比（相对整个 RW2） | 说明 |
|---|---|---|---|---|
| `lossless` | – | f=0，e3，planes | 1.34–1.59x（相对原始数据 1.10–1.20x） | 逐位一致 |
| `archival` | – | f=0 + `--keep-preview small` | 约 1.30–1.54x | 归档，带相机 JPEG |
| `high` | d=0.1 | f=0.5 | ISO100 约 3.9x（估计，约 1.5x d0.2 的体积）；ISO4000 2.36x | 能承受 > +3EV 和重度调色 |
| **`vl`（默认）** | **d=0.2** | **f=1.0** | ISO100 5.78x（实测）；ISO320 约 7–10x（裁切外推）；ISO4000 3.12–3.17x | 后期 +2..+3EV 近视觉无损 |
| `compact` | d=0.3 | f=2.0 | ISO100 7.62x；ISO4000 4.44–4.51x | 高压缩，+3EV 可能看出差异 |

### 4.2 参数表

| 名称 | CLI | 默认 | 范围 | 作用 / 依据 |
|---|---|---|---|---|
| preset | `--preset` | vl | lossless\|archival\|high\|vl\|compact | 见上表 |
| engine | `--engine` | auto | auto\|half3\|nlq\|gat4\|lossless | 第 2.4 节 |
| quality | `-q/--quality` | 由预设决定 | half3/gat4：d 0.05–0.45；nlq：f 0.25–4 | 覆盖预设；对 auto 引擎，必须给成对的 `--d`、`--f` |
| d | `--d` | 0.2 | 0.05–0.45 | half3 主旋钮；≥0.45 时 +3EV 可见损失（A） |
| f | `--f` | 1.0 | 0（无损）、0.25–4 | nlq 步长（以 σ 为单位）；附加噪声 RMS = sqrt(1+f²/12)−1 |
| dD | `--dD` | = d | d..2d | D 平面距离（A：= d 接近最优） |
| snr threshold | `--snr-threshold` | 60 | 10–200 | auto 选择阈值（Q1，已标定） |
| effort | `--effort` | 无损/nlq 3，half3 5 | 1–9 | nlq：≥5 时切到 stack4 e7（小约 4%，慢 8x）；1 = 最快（约大 12%） |
| layout | `--layout` | 由 effort 决定 | planes\|stack4 | nlq/无损 |
| recon | `--recon` | auto | auto\|mid\|centroid | auto：f≥2 用 centroid |
| noise_model | `--noise-model` | auto+iso_cap（有表时） | auto\|auto+iso_cap\|manual:g,s2 | 第 2.3 节 |
| matrix | `--no-matrix` | 开 | flag | half3 使用 sRGB 矩阵（A：率失真更好） |
| satmask | `--no-satmask` | 开 | flag | half3/gat4 的饱和掩码 |
| threads | `--threads` | os.cpu_count() | ≥1 | 单文件内的线程 |
| jobs | `-j/--jobs` | max(1, min(cpu//4, RAM_GB//1.5)) | ≥1 | 批处理进程数 |
| keep-preview | `--keep-preview` | none | none\|small\|full | 第 3.4 节 |
| meta | `--no-meta` | 存 | flag | 省 22–32 KB |
| dng compression | `--dng-compression` | lj92 | lj92\|none12\|none16 | 解码输出 |
| dng tile | `--dng-tile` | 256 | 16 的倍数 | |

---

## 5. CLI、包结构、线程与性能

### 5.1 CLI（argparse，无新依赖）
```
rawsqueeze encode IN... [-o OUT|DIR] [-r] [--preset P] [--engine E] [-q F] [--d D] [--f F] [--dD D]
                  [--effort N] [--layout planes|stack4] [--recon auto|mid|centroid]
                  [--noise-model auto|auto+iso_cap|manual:G,S2] [--snr-threshold T]
                  [--no-matrix] [--no-satmask] [--no-fixup] [--fixup-k K] [--fixup-t T] [--threads N] [-j N]
                  [--keep-preview none|small|full] [--no-meta] [--verify [--no-fallback]]
                  [--overwrite|--skip-existing] [--dry-run] [--json]
rawsqueeze decode IN.rsq... [-o OUT|DIR] [--format dng|npy|pgm16|tiff] [--dng-compression lj92|none12|none16]
                  [--dng-tile N] [--no-exif] [--no-lens-opcode] [--extract-preview] [--threads N] [-j N]
rawsqueeze info IN.rsq [--json]          # HEAD + chunk 表 + 大小 + CRC 校验
rawsqueeze verify IN.rsq --original IN.RW2 [--ev 0,2,3] [--tiles 4|--full]
                  [--metrics psnr,ssimulacra2,butteraugli,noise] [--json]
rawsqueeze bench IN... --sweep 'engine=half3;d=0.1,0.2,0.3' [--crop 2048] [--ev 0,2,3] --csv out.csv
```
- 输出：`.rsq`（encode）；DNG、npy、pgm16 或 16-bit TIFF（decode，TIFF 是 verify 管线显影后的结果）。
- 进度写 stderr，每个文件一行：`[3/57] P1060444.RW2 28.26MB -> 4.89MB (5.78x | raw 4.38x) half3 d0.2 enc 1.6s`，最后输出汇总。
- 退出码：0 成功；1 出错；2 verify 低于阈值。
- `encode --verify`：编码后立即在内存里解码，跑一遍 verify 的快速版（2 个 tile，只看 +3EV）。

### 5.2 包结构
```
rawsqueeze/
  __init__.py      __version__, encode_file, decode_file, verify_file
  cli.py           main(argv=None) -> int
  rawio.py         RawFrame dataclass; load_raw(path) -> RawFrame
  cfa.py           split_planes(m, pat) -> np.ndarray[4,h,w]; merge_planes(P) -> m; color_roles(pat, desc) -> dict; pad_even/crop
  noise.py         NoiseParams(g, s2); estimate_noise(plane, black, white, *, block=8, pct=5, nbins=48) -> NoiseParams;
                   estimate_all(frame, threads) -> list[NoiseParams]; apply_iso_cap(params, make, model, iso)
  noise_table.py   K_CAM = {("Panasonic","DC-S9"): (6.5e-4, 6.5e-4, 6.5e-4, 4e-4)}   # per position, per ISO unit
  curves.py        curve_fwd(x, g, s2, f), curve_inv(y, g, s2, f), build_lut(np_params, f, black, white, offset, q_sat, recon, plane=None, q=None) -> np.ndarray[uint16]
  jxl.py           encode_lossless(a, effort, threads) -> bytes; encode_lossy(a, distance, effort, threads) -> bytes; decode(b, threads) -> np.ndarray
  engines/
    __init__.py    get_engine(name) -> Engine; Engine protocol: encode(frame, params) -> list[Chunk]; decode(head, chunks) -> np.ndarray
    nlq.py         (also lossless when f == 0)
    half3.py
    gat4.py
  select.py        snr18(params, white, black) -> float; choose_engine(frame, noise, opts) -> str
  presets.py       PRESETS: dict[str, Preset]; resolve(opts) -> EncodeParams
  container.py     Chunk(fourcc, payload, critical, zstd); write_rsq(path, chunks); read_rsq(path, *, verify_crc=True) -> RsqFile
  meta.py          ExifTool (stay_open wrapper); make_skeleton(src, raw_bytes) -> (blob, info); jpeg_strip_scan(b);
                   extract_previews(src) -> dict; transfer_exif(skel_blob, dng_path, et)
  dng.py           build_tags(frame_like, bps) -> DNGTags; LJ92DNG(RAW2DNG); write_dng(mosaic, head, path, *, compression, tile, threads) -> None;
                   dng_bytes16(mosaic, head) -> bytes   (verify 管线用)
  verify.py        develop(mosaic, head, ev) -> np.ndarray[uint16 HxWx3]; pick_tiles(img, n, seed); metrics(ref, dist) -> dict;
                   floor_control(m, seed) -> np.ndarray; noise_metrics(...); verify(orig_mosaic, rec_mosaic, head, evs, tiles) -> Report
  bench.py         run_sweep(files, sweep, crop, evs) -> list[dict]
  tools.py         which(name) and version detection: exiftool, cjxl, djxl, ssimulacra2, butteraugli_main
tests/ (第 7 节)
```
公共 API：
```python
def encode_file(src, dst, *, preset="vl", engine="auto", quality=None, d=None, f=None, effort=None,
                threads=None, keep_preview="none", store_meta=True, verify=False) -> EncodeReport
def decode_file(src, dst, *, fmt="dng", dng_compression="lj92", exif=True, threads=None) -> DecodeReport
def encode_frame(frame: RawFrame, params: EncodeParams) -> list[Chunk]
def decode_mosaic(rsq: RsqFile, threads=None) -> np.ndarray
def verify_file(rsq_path, original_path, *, evs=(0,2,3), tiles=4, full=False, metrics=("psnr","ssimulacra2","butteraugli","noise")) -> VerifyReport
```

### 5.3 线程与性能预算（8 核 M 系列，24 MP）

| 步骤 | 时间 | 并行方式 |
|---|---|---|
| rawpy 读取 + 解包 | 0.22 s | – |
| exiftool 元数据 + 骨架 | 0.3 s（批处理用 stay_open 时更少） | 与噪声估计并行（子进程） |
| 噪声估计 | 0.65 s 单线程 → 约 0.2 s | 4 个平面放 ThreadPool |
| nlq/无损 JXL e3 | 0.21 s | ThreadPool(4) × numthreads=threads//4 |
| half3 JXL e5 | 0.62 s | rgb 与 D 两个线程 |
| sha256 | 约 0.05 s | – |
| **编码合计** | **约 1.2–1.6 s** | |
| JXL 解码 | 0.11–0.28 s | 按平面并行 |
| LJ92 DNG | 0.33 s → 约 0.1 s | tile 线程池 |
| exiftool 搬运 | 0.35 s | stay_open |
| **解码到 DNG 合计** | **约 0.7–0.9 s** | |

- 内存：每个 worker 峰值约 1 GB（half3 的 float32 路径再多约 0.3 GB）。按 RAM 限制 `-j`。
- 批处理：ProcessPoolExecutor(jobs)，每个 worker 用 `threads = cpu//jobs`，并持有一个 stay_open exiftool。

---

## 6. 验证与基准测试

### 6.1 显影管线（verify 和 bench 共用，必须确定性）
- 原始马赛克和重建马赛克走**完全相同**的路径：`dng_bytes16(mosaic, head)`（pidng 未压缩 16-bit，在内存里生成，0.01 s）→ `rawpy.imread(io.BytesIO(buf))` → `postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16, exp_shift=2.0**ev, exp_preserve_highlights=0.0, gamma=(2.4,12.92), user_flip=0, demosaic_algorithm=AHD, highlight_mode=Clip)` → 按 DefaultCrop 裁切。
- **不要**拿 rawpy 对 RW2 本身的渲染来比。同一个马赛克，RW2 和 DNG 两条路径之间也只有 93–97 dB（AHD 方向判断在约 12k 个像素上翻转）。DNG 对 DNG 的路径是确定性的。
- EV 取 {0, +2, +3}。LibRaw 的 exp_shift 范围是 0.25..8，+3EV 已经是上限；+4EV 或提阴影曲线要在 numpy 里对线性输出缩放（`gamma=(1,1)` 输出线性数据后再乘增益并做 sRGB OETF，B 的方法）。
- 可选附加管线 `--dev bilinear`：A 的 numpy 简易显影，用于快速扫描；注意它比 AHD 乐观。

### 6.2 指标
1. 图像写成 16-bit 二进制 PPM（0.1 s；PNG 需要 5 s）。
2. `ssimulacra2 ref.ppm dist.ppm`（取最后一个 token）。`butteraugli_main ref.ppm dist.ppm --pnorm 3`（第 1 行是 max，`3-norm: x` 那行是 p3）。PSNR 在 16-bit 上计算。
3. tile：默认 4 个 2048² tile（中心、平均亮度最暗、局部方差最高、带种子的随机位置），每个 EV 报**最差 tile**。`--full` 时用全幅（每个 EV 约 15 s）。
4. B 的噪声相对指标（便宜，主要服务 nlq 和高 ISO）：
   - raw 域：按 CFA 通道和亮度 bin（16 个）计算 `RMSE(R−M)/σ_model(M)`。
   - +3EV 时的平均 sRGB-8bit 偏置（中心 tile 和最暗 tile）。
   - +3EV 时高通噪声 std 比（重建 / 原始，在平坦区域测）。
   - ss2 的 ds2 版本（2×2 面积下采样）。
5. FLOOR 对照：原始马赛克 + 随机 0/1 DN（固定种子），走同样的管线和指标，与结果一起报告，用来判断某个分数是否已在"颗粒重实现"的噪声底之内。
6. 高光裁切一致性：`M ≥ wl` 的像素必须满足 `R == wl`。报告不一致的像素数（期望为 0）。
7. JSON 报告包含：引擎、参数、MB、两个口径的比、编码/解码时间、每个 EV 的 {ss2, ba_max, ba_p3, psnr} 最差值、FLOOR 值、噪声指标、裁切一致性。

### 6.3 bench
`bench --sweep` 对 `engine × 参数` 做笛卡尔积。可以只编码 `--crop` 裁切（各引擎都支持对裁切后的子马赛克编码，裁切起点对齐到偶数）。输出 CSV 列：`file,iso,engine,param,effort,bytes,ratio_file,ratio_raw,enc_s,dec_s,ev,ss2,ss2_ds2,ba_max,ba_p3,psnr,bias8,noise_ratio,floor_ss2`。

### 6.4 各预设的验收目标（verify 退出码 2 的判据；默认 4 个 tile 取最差）

**2026-10 重标定**（13 张 DC-S9 样张，ISO 100–51200；实现 `verify.nlq_criteria` / `verify.half3_criteria`）：判据是引擎质量参数的函数，按文件 HEAD 里实际的 f（nlq）或 d（half3）计算；preset 只在 HEAD 没有参数时提供名义值（high: d0.1/f0.5，vl: d0.2/f1，compact: d0.3/f2）。原表（v1 规格）中的若干阈值低于理论下限或把肉眼看不出问题的低 ISO half3 文件判为失败，已替换。

| 引擎 | 判据 | 说明 |
|---|---|---|
| lossless / archival（或 nlq f=0） | 马赛克 sha256 一致；DNG 重读逐位一致 | 13/13 通过 |
| nlq f | raw 域 RMSE/σ ≤ 1.2f/√12 + 0.03；+3EV 噪声 std 比 ≤ √(1+(1.2f)²/12) + 0.03；\|bias8\| ≤ 0.75 + 0.25f²；f ≤ 0.5 且低 ISO（SNR18 ≥ 40 或 ISO ≤ 800）时另加每个 EV 的 ss2 ≥ FLOOR − 3 | 理论值（均匀量化步长 fσ）加估计器余量 ×1.2 / +0.03。f=0.5 / 1 / 2 → 0.203 / 0.376 / 0.723，1.045 / 1.088 / 1.247，0.81 / 1.00 / 1.75。f=1 实测 13 张：RMSE/σ 0.285–0.328，噪声比 1.009–1.066，\|bias8\| 0.009–0.647 |
| half3 d | ss2(0/+2/+3EV) ≥ 锚点值，ba p3(+3EV) ≤ 锚点值，ba max(+3EV) ≤ 锚点值，裁切一致性 = 0；锚点之间按 d 线性插值，两端按外侧斜率外推，d 限定在 [0.05, 0.6] | 锚点见下表。+3EV ss2 是主要分界，+2EV ss2 第二；0EV ss2 分不开好坏，只作底线。half3 不用噪声比（P1060384 读数 0.71 但看不出问题） |
| gat4 | 无判据（实验性，verify 只报告） | |

half3 锚点（好 = ISO 100–320 三张，4×/+3EV 看不出差异；坏 = 强制 half3 的 ISO ≥ 640，颗粒被压平）：

| d（预设） | ss2 0 / +2 / +3EV ≥ | ba p3 +3EV ≤ | ba max +3EV ≤ | 好文件最差值（ss2 0/+2/+3，p3） | 坏文件最好值（ISO800） |
|---|---|---|---|---|---|
| 0.1（high） | 84 / 82 / 78 | 1.2 | 6 | 88.1 / 85.0 / 81.8，0.90 | 88.6 / 81.1 / 76.2，1.06 |
| 0.2（vl） | 80 / 77 / 72 | 1.5 | 8 | 84.3 / 79.6 / 76.8，1.23 | 85.3 / 74.9 / 68.3，1.37 |
| 0.3（compact） | 78 / 73 / 67 | 1.8 | 9 | 81.2 / 75.4 / 71.7，1.52 | 82.8 / 70.3 / 62.4，1.57 |

v1 原判据（仅供对照）：high/half3 +3EV ss2 ≥ 85、p3 ≤ 0.8；high/nlq 噪声比 ≤ 1.02、\|bias\| ≤ 0.3；vl/half3 0EV ss2 ≥ 84、+3EV ss2 ≥ 79、p3 ≤ 1.0；vl/nlq 噪声比 ≤ 1.05、\|bias\| ≤ 0.5、RMSE/σ ≤ 0.32；compact/half3 +3EV ss2 ≥ 76、p3 ≤ 1.3；compact/nlq 噪声比 ≤ 1.18、\|bias\| ≤ 0.5。

---

## 7. 测试计划（pytest；需要把 pytest 加为 dev 依赖，见第 8.3 节）
测试数据：`tests/fixtures/` 下用 pidng 从样张生成 512×512 的小 DNG（rawpy 可读，不必依赖 30 MB 样张），另外用合成 Poisson-Gaussian 马赛克。用到完整样张的测试标 `@pytest.mark.slow`。

1. `test_cfa.py`：所有 4 种 2×2 排列下 split/merge 往返一致；颜色角色映射（RGBG + [[0,1],[3,2]] → R(0,0) G1(0,1) G2(1,0) B(1,1)）；奇数尺寸的补边和裁回。
2. `test_curves.py`：恒等区精确（x<x0 时 y==x）；curve_inv(curve_fwd(x)) ≈ x；LUT 单调；`LUT[q_sat]==wl`；负值偏移可逆；f→0 时退化为无损。
3. `test_noise.py`：在合成 var=g·x+s2 数据（g ∈ {0.06, 0.6, 2.4}）上，估计的 g 在 ±10% 以内、s2 在合理范围内；ISO 上限生效；纹理较重的合成图上 g 不会被低估。
4. `test_nlq.py`：在合成数据上误差 RMS ≈ f/√12·σ（±10%）；平均偏置约 0；qmax<256 时走 uint8 路径；饱和像素恢复为 wl；centroid LUT 落在 bin 内。
5. `test_lossless.py`（参数化：每个 fixture、奇数尺寸 6000×4002、3×3、全饱和、全黑、含 > wl 的值）：逐位一致，sha256 一致。
6. `test_half3.py`：常数图和渐变图重建误差有界；饱和掩码精确；矩阵为零时回退到 M=I；HEAD 里的 M/Minv 在 float64 下往返一致；解码不调用 rawpy。
7. `test_select.py`：SNR18 计算；阈值两侧选到的引擎；非 Bayer 输入被拒绝或回退。
8. `test_container.py`：每种 chunk 单 bit 翻转都报 CRC 错；随机位置截断报错；magic 错、major 不支持都报错；未知非关键 chunk 被跳过，未知关键 chunk 报错；INDX 丢失时能扫描重建。
9. `test_dng.py`：rawpy 打开后 raw_image == 马赛克；black/white/pattern/WB/crop 往返；LJ92 边缘 tile；`exiftool -validate` 无错误；（slow）Panasonic/GPS 标签数与原文件一致；DNG 对 DNG 的显影是确定性的。
10. `test_meta.py`：`jpeg_strip_scan` 保留所有 marker；（slow）骨架的标签数与原文件一致（329）。
11. `test_verify.py`：重建 == 原始时 PSNR=inf、ss2=100；FLOOR 对照可复现；裁切一致性计数正确。
12. `test_cli.py`：在 fixture 上跑通 encode/decode/info/verify；`--skip-existing`；`-j 2`；退出码。
13. （slow）`test_presets_regression.py`：在 P1060444 和 P1037920 上，各预设的大小落在参考值 ±10% 以内，verify 达到第 6.4 节的判据。

---

## 8. 风险、缓解措施与开放问题

### 8.1 主要风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | half3 在高噪声下灾难性失败（ISO4000 AHD 显影 butteraugli 11.7/22.2），而 800–3200 之间没有数据 | 阈值 SNR18 ≥ 60（已用中 ISO 样张标定，Q1）；auto 只在 half3 实测良好的区间启用；`encode --verify` 发现超出判据时自动回退到 nlq（仅在 `encode --verify` 时生效，可用 `--no-fallback` 关闭；代价是一次快速 verify，约 3 s） |
| R2 | 噪声估计在纹理丰富场景高估 g（PANA9831 高 2.3x），而且其它相机没有 ISO 上限表 | ISO 上限表；输入为 DNG 时读 NoiseProfile；`--noise-model manual`；高估的方向是安全的（落到 nlq 或步长变大），并在 verify 中报告 |
| R3 | "视觉无损"判据：vl/half3 d0.2 的 +3EV ss2 约 81，低于严格的 85；高 ISO 下全参考指标失效 | 判据按引擎分开（第 6.4 节），并报 FLOOR 对照与噪声相对指标；提供 `high` 预设；实现后做一次人工 A/B 目视检查（Q4） |
| R4 | 高光：half3 在部分饱和 quad 处误差最高约 900 DN；非掩码像素可能产生假饱和或洋红色；nlq LUT 舍入可能让裁切值低于 wl | SATM 掩码 + nlq 饱和码；verify 检查裁切一致性；`--no-satmask` 只用于调试 |
| R5 | 元数据和 DNG 的通用性：骨架策略依赖 RW2 的 RawDataOffset 和"MakerNotes 在 JpgFromRaw APP1"；pidng 有各种怪癖；DNG 只有 ColorMatrix1，Lightroom 渲染可能与原生 RW2 配置不同 | 骨架定位按格式分派，失败走 exif-only 兜底；只保留 LJ92DNG 子类这一条写入路径；rgb_xyz_matrix 为零时从骨架读或拒绝写 DNG |
| R6 | half3/gat4 的浮点解码在不同 libjxl 版本或平台间不逐位一致 | 记录 libjxl 版本；recon_sha256 只作提示；需要逐位确定的场合用 nlq |
| R7 | JXL XYB 会把低于约 −0.0038 的线性值钳掉，深色饱和色暗部有约 1 DN 的正偏置 | 实测影响很小；以开放问题 Q6 跟踪（是否加 pedestal）。**补充（2026-10 review）**：更严重的是 libjxl 把 opsin 混合值 `OPSIN·rgb + 0.0038` 钳到 ≥ 0，饱和蓝/青色 LED 会被系统性改写（ISO640 样张 raw 偏差 R +67 DN）。修复：编码时计算每个位点所需的混合系数，把 M 向 I 混合到最小的 α 使 opsin 混合值非负（允许 1e-6 比例的孤立位点例外），α 写入 HEAD `matrix_blend`，解码器只用 Minv，格式不变 |
| R8 | 黑电平的特殊情况：LibRaw 二维 cblack 图案、低于黑电平的负噪声、非 Bayer CFA | nlq 用负值偏移；half3 在 [0,1] 裁剪（低噪声文件影响很小）；不支持的布局只做无损 |
| R9 | 裁切实验（bilinear）比全幅 AHD 乐观（1.84x vs 1.39x） | 所有默认值以全幅 AHD 结果为准；bench 默认用 AHD |

### 8.2 开放问题（实现者按推荐默认值实现，之后用 bench 标定）

| # | 问题 | 推荐默认 |
|---|---|---|
| Q1 | half3/nlq 的切换阈值（中 ISO 无数据） | **已解决**：SNR18=60（≈ DC-S9 ISO450），由 13 张 ISO 100–51200 样张标定（docs/STATUS.md「阈值标定」，第 2.4 节） |
| Q2 | 按 tile 混合引擎（A 的建议） | v1 不做；容器保留 `TILE` |
| Q3 | 中 ISO 和 compact 场景下 gat4 是否优于 half3 或 nlq | gat4 保持实验性；必须先通过全幅 AHD + butteraugli 验证 |
| Q4 | vl 的 half3 d 取 0.2 还是 0.15 | 0.2（压缩比优先，指标与 sqrt-k2 基线持平）；如果人工目视在 +3EV 下看出差异，改为 0.15 |
| Q5 | centroid LUT 的启用门槛 | f ≥ 2 |
| Q6 | half3 是否加 +0.05 pedestal 来避免负值钳位 | 关（会让 XYB 把暗部当成更亮的区域，导致暗部量化变粗；未测） |
| Q7 | 其它相机的 ISO 上限表 | 不在表里就不设上限；可以从 DNG 的 NoiseProfile 推导 k |
| Q8 | 无损的"极限"档（cjxl -e9 -g3，约 −5%，45–55 s） | v1 不做；以后可加 `--effort 10` 调用 cjxl 子进程 |
| Q9 | 非 Bayer 的无损 | 回退为按 CFA 周期做 polyphase；X-Trans（6×6，36 个平面）的效率未测 |
| Q10 | DNG 是否标记为有损 | 写 Software 字符串，并写 XMP `rawsqueeze:Engine/Param`；不使用 DNG 的 LinearRaw/有损标志 |
| Q11 | half3 effort 默认 5 还是 7 | 5（A：率失真相同、快 35%，e7 = e9） |

### 8.3 依赖
- 运行时不需要新依赖（argparse、zlib、json、hashlib、concurrent.futures，加上已安装的 zstandard/imagecodecs/pidng/rawpy/numpy）。
- 外部 CLI：exiftool（META 和 EXIF 搬运；缺失时降级为 `--no-meta`，并给出警告）、cjxl/djxl（仅用于预览转码）、ssimulacra2 和 butteraugli_main（仅 verify/bench 使用）。
- 请求 dev 依赖：`pytest`（测试）。可选：`tqdm`（仅外观）。

---

## 9. 附录：设计者的代码片段与陷阱（尽量原样保留）

### A.1 half3 编解码（A，已验证）
```python
import numpy as np, imagecodecs as ic, zstandard, rawpy
XYZ_FROM_SRGB = np.array([[0.4124564,0.3575761,0.1804375],[0.2126729,0.7151522,0.0721750],[0.0193339,0.1191920,0.9503041]])
with rawpy.imread(path) as r:
    m = r.raw_image.astype(np.int32); black = int(r.black_level_per_channel[0]); white = int(r.white_level)
    wb = np.array(r.camera_whitebalance[:3], float); wb /= wb[1]
    c = np.array(r.rgb_xyz_matrix[:3]) @ XYZ_FROM_SRGB; c /= c.sum(1, keepdims=True); M = np.linalg.inv(c)  # srgb_from_cam
m = np.minimum(m, white)                       # 4080..4095 -> 4079
nrm = lambda p: np.clip((p.astype(np.float32)-black)/(white-black), 0, 1)
R, G1, G2, B = nrm(m[0::2,0::2]), nrm(m[0::2,1::2]), nrm(m[1::2,0::2]), nrm(m[1::2,1::2])  # pattern [[0,1],[3,2]] RGBG
rgb = (np.stack([R*wb[0], (G1+G2)/2, B*wb[2]], -1) @ M.T).astype(np.float32)   # NOT clipped
b_rgb = ic.jpegxl_encode(np.ascontiguousarray(rgb), distance=d, effort=5, numthreads=8)  # float -> signalled linear sRGB
b_D   = ic.jpegxl_encode((G1-G2+0.5).astype(np.float32), distance=d, effort=5, numthreads=8)
b_sat = zstandard.ZstdCompressor(level=19).compress(np.packbits(m >= white).tobytes())
# decode
rgb = ic.jpegxl_decode(b_rgb, numthreads=8).astype(np.float64) @ np.linalg.inv(M).T
D = ic.jpegxl_decode(b_D).astype(np.float64) - 0.5
den = lambda x: np.clip(np.rint(x*(white-black)+black), black, white).astype(np.uint16)
out = np.empty((2*rgb.shape[0], 2*rgb.shape[1]), np.uint16)
out[0::2,0::2] = den(rgb[...,0]/wb[0]); out[0::2,1::2] = den(rgb[...,1]+D/2)
out[1::2,0::2] = den(rgb[...,1]-D/2);   out[1::2,1::2] = den(rgb[...,2]/wb[2])
mask = np.unpackbits(np.frombuffer(zstandard.ZstdDecompressor().decompress(b_sat), np.uint8))[:out.size].reshape(out.shape).astype(bool)
out[mask] = white
```
> 产品化时要改的地方：黑电平按位置取；颜色角色由 pat/desc 推导；M/Minv 存进 HEAD；mask 用原始（钳位前）马赛克计算（钳位后 `>= white` 结果相同）。

陷阱（A）：
- imagecodecs.jpegxl_encode 对 uint16 和 float32 的灰度/RGB 输入默认都信令为 LINEAR（jxlinfo 显示 'Transfer function: Linear'）；`transfer=13` 是 sRGB。sRGB 编码数据配 sRGB 信令，与线性数据配线性信令，大小和质量都相同。
- float32 中 >1 的值能保留（HDR）；低于约 −0.0038 的线性值会被 XYB 钳掉。
- effort 7 == effort 9（d≤0.3 时字节相同）；effort 5 与之相差 0.5% 以内。d≤0.3 时 cjxl 的 --epf/--gaborish 不起作用。相同 d/effort 下 cjxl 与 imagecodecs 输出逐字节相同。
- cjxl 的 float 输入：PFM（'PF'/'Pf'，scale −1.0，行从下到上，小端），加 `-x color_space=RGB_D65_SRG_Rel_Lin`（灰度用 `Gra_D65_Rel_Lin`）。
- 用 JXL lossless 存饱和掩码，即使几乎为空也有约 5 KB 开销；zstd(packbits) 只要 78 B 到 8 KB。
- gain（预缩放）只是质量旋钮，没有率失真收益；dD 与 d 不同也没有收益；在 R/B 或 G 上用更粗的 d 也没有收益。

### A.2 nlq 曲线与 LUT（B，已验证）
```python
def curve_fwd(x, g, s2, f):          # x = raw - black (float)
    x = np.maximum(x, 0)             # (rawsqueeze: replace by negative-offset handling, see 2.2 step 4)
    x0 = max((1.0/(f*f) - s2)/g, 0.0); c0 = np.sqrt(g*x0 + s2)
    return np.where(x < x0, x, x0 + (2.0/(g*f))*(np.sqrt(g*x + s2) - c0))
def curve_inv(y, g, s2, f):
    x0 = max((1.0/(f*f) - s2)/g, 0.0); c0 = np.sqrt(g*x0 + s2)
    t = (y - x0)*(g*f/2.0) + c0
    return np.where(y < x0, y, (t*t - s2)/g)
q = np.round(curve_fwd(P.astype(np.float64) - black, g, s2, f)).astype(np.uint16)
blob = imagecodecs.jpegxl_encode(q, lossless=True, effort=3)
lut = np.clip(np.round(curve_inv(np.arange(int(q.max())+1, dtype=np.float64), g, s2, f) + black), 0, white).astype(np.uint16)
xhat = lut[imagecodecs.jpegxl_decode(blob).reshape(q.shape)]
# centroid LUT: lut[k] = round(mean(P[q==k])) via np.bincount(q.ravel(), P.ravel())/np.bincount(q.ravel())
```

### A.3 噪声估计器 estimate_noise2（B，原样）
```python
import numpy as np
def _block_sig(x, B):
    r = x[1:-1,1:-1] - 0.25*(x[:-2,1:-1]+x[2:,1:-1]+x[1:-1,:-2]+x[1:-1,2:])
    r = r/np.sqrt(1.25)
    xm=x[1:-1,1:-1]
    h=(r.shape[0]//B)*B; w=(r.shape[1]//B)*B
    rb=r[:h,:w].reshape(h//B,B,w//B,B).transpose(0,2,1,3).reshape(-1,B*B)
    mb=xm[:h,:w].reshape(h//B,B,w//B,B).transpose(0,2,1,3).reshape(-1,B*B)
    sig=1.4826*np.median(np.abs(rb-np.median(rb,1,keepdims=True)),1)
    return mb, sig
_CF={}
def pct_bias(B,pct):
    if (B,pct) not in _CF:
        z=np.random.default_rng(0).standard_normal((1024,1024)).astype(np.float32)
        _,s=_block_sig(z,B); _CF[(B,pct)]=np.percentile(s,pct)**2
    return _CF[(B,pct)]
def estimate_noise2(p, black, white=4079, B=8, pct=5, nbins=48):
    x=p.astype(np.float32)-black
    mb,sig=_block_sig(x,B)
    mean=mb.mean(1); ok=(mb.max(1)<0.95*(white-black))&(mb.min(1)>0)   # avoid clip at both ends
    mean,sig=mean[ok],sig[ok]
    cf=pct_bias(B,pct)
    edges=np.quantile(mean,np.linspace(0,1,nbins+1))
    ms=[];vs=[]
    for a,b in zip(edges[:-1],edges[1:]):
        s=(mean>=a)&(mean<b)
        if s.sum()<50: continue
        ms.append(np.median(mean[s])); vs.append(np.percentile(sig[s],pct)**2/cf)
    ms=np.array(ms);vs=np.array(vs)
    A=np.stack([ms,np.ones_like(ms)],1)
    # lower-envelope init: keep bins whose v/(m+20) ratio is in the lowest 40%
    ratio=vs/(ms+20); w=np.where(ratio<=np.quantile(ratio,0.4),1/np.maximum(vs,0.5),0)
    for it in range(4):   # iteratively drop bins far above fit (texture-contaminated)
        sol,*_=np.linalg.lstsq(A*w[:,None],vs*w,rcond=None)
        pred=A@sol; w=np.where(vs>1.3*np.maximum(pred,0.5),0,1/np.maximum(pred,0.5))
    return max(sol[0],1e-4),max(sol[1],0.25),ms,vs
```
> 产品化时：若有效 bin 少于 4 个（图像过暗或过亮），回退为 ISO 先验（有相机表时）或 `g = 1e-4·ISO`，并在 HEAD 里标记 `noise.fallback=true`。

### A.4 DNG 标签（C，dng_proto.py 原样）
```python
from pidng.core import RAW2DNG, DNGTags, Tag, DNG, dngIFD, dngTag
from pidng.defs import Compression, DNGVersion, PhotometricInterpretation, CalibrationIlluminant
def srat(x, den=10000):
    return [int(round(x*den)), den]
def build_tags(m, bps, make='Panasonic', model='DC-S9', tile=None):
    t=DNGTags(); H,W=m['H'],m['W']
    t.set(Tag.ImageWidth,W); t.set(Tag.ImageLength,H)
    tw,th = tile if tile else (W,H)
    t.set(Tag.TileWidth,tw); t.set(Tag.TileLength,th)
    t.set(Tag.Orientation,1)
    t.set(Tag.PhotometricInterpretation,PhotometricInterpretation.Color_Filter_Array)
    t.set(Tag.SamplesPerPixel,1); t.set(Tag.BitsPerSample,bps)
    t.set(Tag.CFARepeatPatternDim,[2,2]); t.set(Tag.CFAPattern,m['cfa'])
    t.set(Tag.CFAPlaneColor,[0,1,2]); t.set(Tag.CFALayout,1)
    blk=[m['black'][c] for c in np.array(m['pattern']).flatten()]   # per CFA POSITION
    t.set(Tag.BlackLevelRepeatDim,[2,2]); t.set(Tag.BlackLevel,blk)
    t.set(Tag.WhiteLevel,m['white'])
    t.set(Tag.ColorMatrix1,[srat(v) for row in m['xyz2cam'] for v in row])
    t.set(Tag.CalibrationIlluminant1,CalibrationIlluminant.D65)
    wb=m['wb']; t.set(Tag.AsShotNeutral,[[int(round(wb[1]*1000)),int(round(wb[0]*1000))],[1,1],[int(round(wb[1]*1000)),int(round(wb[2]*1000))]])
    t.set(Tag.BaselineExposure,[[0,100]])
    t.set(Tag.Make,make); t.set(Tag.Model,model); t.set(Tag.UniqueCameraModel,f'{make} {model}')
    t.set(Tag.ActiveArea,[0,0,H,W])
    cl,ct,cw,ch=m['crop']; t.set(Tag.DefaultCropOrigin,[cl,ct]); t.set(Tag.DefaultCropSize,[cw,ch])
    return t
```
> 产品化时：Orientation 由 flip 映射；Make/Model 取自 HEAD；ActiveArea 取自 margins。

### A.5 LJ92 tile DNG（C，原样）
```python
class LJ92DNG(RAW2DNG):
    tile=(256,256)
    def __process__(self, raw, tags, compress):
        W=tags.get(Tag.ImageWidth).rawValue[0]; H=tags.get(Tag.ImageLength).rawValue[0]; bps=tags.get(Tag.BitsPerSample).rawValue[0]
        tw,th=self.tile
        nx=-(-W//tw); ny=-(-H//th)
        pad=np.zeros((ny*th,nx*tw),np.uint16); pad[:H,:W]=raw
        tiles=[]
        for y in range(ny):
            for x in range(nx):
                t=pad[y*th:(y+1)*th, x*tw:(x+1)*tw]
                t2=np.ascontiguousarray(t.reshape(th//2, tw*2))  # two sensor rows per LJ92 row -> above-predictor sees same CFA colour
                tiles.append(imagecodecs.ljpeg_encode(t2, bitspersample=bps))
        d=DNG(); d.ImageDataStrips=tiles
        ifd=dngIFD(); off=dngTag(Tag.TileOffsets,[0]*len(tiles)); ifd.tags.append(off)
        ifd.tags.append(dngTag(Tag.NewSubfileType,[0]))
        ifd.tags.append(dngTag(Tag.TileByteCounts,[len(x) for x in tiles]))
        ifd.tags.append(dngTag(Tag.Compression,[Compression.LJ92]))
        ifd.tags.append(dngTag(Tag.Software,"rawsqueeze-proto"))
        ifd.tags.append(dngTag(Tag.DNGVersion,DNGVersion.V1_4)); ifd.tags.append(dngTag(Tag.DNGBackwardVersion,DNGVersion.V1_1))
        for tg in tags.list(): ifd.tags.append(tg)
        d.IFDs.append(ifd); n=d.dataLen()
        off.setValue([k for _,k in d.StripOffsets.items()])
        buf=bytearray(n); d.setBuffer(buf); d.write(); return buf
```
陷阱（C）：`imagecodecs.ljpeg_encode` 只接受 2-D（或 H×W×1）uint16，没有 predictor 参数，传 H×W×2 会报 'invalid data shape or dtype'。`__process__` 名字两端都有双下划线，所以不触发名字改编，子类可以直接覆盖。

### A.6 元数据骨架（C）
```python
def jpeg_strip_scan(b):
    i=2; b=bytearray(b)
    while i < len(b):
        mk=b[i+1]; L=int.from_bytes(b[i+2:i+4],'big')
        if mk==0xDA: end=i+2+L; b[end:len(b)-2]=bytes(len(b)-2-end); return b, end
        i+=2+L
rdo=json.loads(subprocess.run(['exiftool','-j','-n','-RawDataOffset',src],capture_output=True).stdout)[0]['RawDataOffset']
hdr=bytearray(data[:rdo])
for tag in ('JpgFromRaw2','JpgFromRaw'):
    b=subprocess.run(['exiftool','-b','-'+tag,src],capture_output=True).stdout; o=data.find(b); hdr[o:o+len(b)]=jpeg_strip_scan(b)[0]
blob=zstd.ZstdCompressor(level=19).compress(bytes(hdr))   # 22.7-32.4 KB
```
> 产品化时：`jpeg_strip_scan` 要处理 marker 前的填充字节 0xFF、没有长度字段的 marker（RSTn、SOI、TEM）以及多个 SOS（渐进式 JPEG）；`data.find` 找不到时跳过该预览并给出警告。

### A.7 容器写入（C）
```python
MAGIC=b'\x89RSQ\r\n\x1a\n'; VER=(1,0)
CH=struct.Struct('<4sIQ')                          # fourcc, flags(bit0=zstd, bit1=critical), u64 length
def chunk(f, cc, payload, flags=0):
    h=CH.pack(cc,flags,len(payload)); f.write(h); f.write(payload); f.write(struct.pack('<I', zlib.crc32(payload, zlib.crc32(h))))
# file = MAGIC + <HH major,minor> + chunk* + INDX chunk + footer('<Q4s', offset_of_INDX, b'RSQE')
```

### A.8 验证显影（C/A/B）
```python
buf=bytes(RAW2DNG_with_tags16.convert(mosaic))          # filename='' -> returns bytearray, 0.01 s
with rawpy.imread(io.BytesIO(buf)) as r:
    img=r.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16, exp_shift=2.0**ev, exp_preserve_highlights=0.0,
                      gamma=(2.4,12.92), user_flip=0, demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD, highlight_mode=rawpy.HighlightMode.Clip)
img=img[ct:ct+ch, cl:cl+cw]
open(p,'wb').write(b'P6\n%d %d\n65535\n'%(w,h)+img.astype('>u2').tobytes())   # 16-bit PPM: 0.1 s vs PNG 5 s
# ssimulacra2 a.ppm b.ppm ; butteraugli_main a.ppm b.ppm --pnorm 3   # line1 = max, last line '3-norm: x'
```
- 另一种可行做法（A、B 实测）：直接原地修改 `r.raw_image[:] = rec` 再调 postprocess，LibRaw 会采用修改后的数据。但 RW2 路径与 DNG 路径之间存在 AHD 差异，所以 verify 统一走 DNG 对 DNG。
- 注意参数拼写是 `exp_preserve_highlights`；exp_shift 范围 0.25..8。

### A.9 其它陷阱汇总
- cjxl 的 PGM 输入要求 maxval 为 2^n−1（4095/65535），否则报 'Getting pixel data failed'（B）。
- 最好的 cjxl 无损参数是 `cjxl in.pgm out.jxl -d 0 -e 7 -P 6 -I 100 -g 3`，只比 imagecodecs e3 好约 1.3%，却慢 30–50 倍（B）。
- 无损时不要做可逆色彩去相关（lifting），会大 4–5%，因为噪声在每个感光点上独立（B）。
- 不要把整幅马赛克当一张 2D 图编码（+34%）（B）。
- 对 nlq 的 q 平面，zstd-19 比 JXL modular 大 14–30%（B）。
- LJ92 tile 可以并行编码（imagecodecs 释放 GIL）；JXL 编码同样释放 GIL（C 实测 ThreadPool 有效）。
- RW2 解析要点：嵌入 JpgFromRaw 的 APP1 中带有 ThumbnailImage（7,801 B），骨架方式会把它一并保留（C）。
- zsh 下多词参数要显式传递，需要分词时用 `${=var}`（A）。
- 实验脚本：A `perceptual-codec/{lib.py,codecs_.py,exp1..exp9b,dbg1..3}.py`；B `noise-adaptive-quantizer/{ll1,ll2,ll3,sweep2,sweep3,calib,coder,noise2,final,lossytest}.py`；C `systems-format/{meta_blob,exif2,dng_proto,exif_transfer,jxl_dng,jxl_threads,rsq_proto,harness,ppm_test}.py`。
