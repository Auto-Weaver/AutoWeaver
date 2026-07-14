# 迁移指南：0.10.x → 0.11.0

> **状态：已落地**——本指南反映最终契约。

0.11.0 把 pipeline 层从"检测容器钦定字段"改成**容器零约束、约束长在消费者身上**。核心一句话：`PipelineContext` 对载荷不再有任何假设，谁要看载荷内部谁就在自己旁边声明所需能力。

## 决策原则

- **容器零地板**——框架层不再规定"所有检测都必须有的字段"
- **直接 break，不留兼容期**——和历次一致，不留 deprecation 别名
- `YOLOSegStep` 输出位置搬家到 `ctx.detections`，`ctx.metadata["segments"]` **直接删除，无过渡期**（无外部消费者）

## Break 总表（速查）

| 旧 | 新 |
|---|---|
| `PipelineContext`（`detections: List[Detection]`） | `PipelineContext[D]`（`detections: List[D]`，载荷泛型） |
| `ProcessStep`（不带类型参数） | `ProcessStep[D]`（泛型，不带参数仍可用） |
| NMS/Filter/Sort 依赖 `Detection` 具体类 | 依赖 `BoxLike` 结构协议（`@runtime_checkable`） |
| `YOLOSegStep` 写 `ctx.metadata["segments"]`（`SegmentResult`，全幅 mask） | 写 `ctx.detections`（`SegmentDetection`，**bbox 局部** mask）；`metadata["segments"]` 删除 |
| `SegmentResult` | 删除，替换为 `SegmentDetection(RegionDetection)` |
| `FilterStep` 配 `classes` 即 `AttributeError` | 修复（把 `object_type` 当 str 用） |

## 新概念

### 1. `PipelineContext[D]` / `ProcessStep[D]` 泛型化

容器对载荷零假设。不带类型参数的旧写法照常工作：

```python
# 仍然合法（D 视作 Any）
ctx = PipelineContext()

class MyStep(ProcessStep):
    def process(self, ctx):
        ...
        return ctx

# 想标注载荷类型时
ctx = PipelineContext[Detection]()

class SegStep(ProcessStep[SegmentDetection]):
    def process(self, ctx: PipelineContext[SegmentDetection]):
        ...
```

### 2. `BoxLike` 协议（消费点声明能力，非框架地板）

需要看载荷内部的 step，在自己旁边用 `typing.Protocol` 声明所需字段。核心包的 NMS/Filter/Sort 只用 `bbox` / `object_type` / `confidence`，于是 `postprocess.py` 里定义：

```python
@runtime_checkable
class BoxLike(Protocol):
    bbox: BoundingBox
    object_type: str
    confidence: float
```

任何暴露这三样的对象都满足它（结构匹配，`isinstance(x, BoxLike)` 可用）。`Detection` / `RegionDetection` 恰好满足，但**框架不强制**任何载荷满足它。自定义 step 若需要别的字段，就在自己那边声明自己的 Protocol。

### 3. `Detection` / `RegionDetection` 是便利实现，不是强制基类

- `Detection` 保留、继续导出，恰好满足 `BoxLike`，用不用随项目。
- 新增 `RegionDetection(Detection)`：带一张 **bbox 局部坐标**的 mask（`(h, w)` uint8 0/255，尺寸=包围盒，不是全幅）+ `area_px`（非零像素缓存）。全幅 mask 在 4000×3000 上每张 ~12 MB，不可接受；需要全幅时把局部 mask 贴回 `(bbox.x1, bbox.y1)` 重建即可。
- dataclass 用 `kw_only=True`：基类尾部有带默认值的字段（`detection_id`），子类两个字段无默认值，否则 dataclass 报 "non-default argument follows default argument"。

## 业务侧迁移步骤

### YOLOSegStep 的输出搬家

```python
# 0.10.x（旧）
result = pipeline.run(ctx)
segments = ctx.metadata["segments"]          # List[SegmentResult]，全幅 mask
for s in segments:
    mask = s.mask                            # (H, W) 全幅
    name = s.class_name

# 0.11.0（新）
result = pipeline.run(ctx)
segments = [d for d in ctx.detections if isinstance(d, SegmentDetection)]
for d in segments:
    mask = d.mask                            # bbox 局部 (h, w)
    name = d.object_type                     # 原 class_name
    cid = d.class_id                         # 保留的额外字段
# ctx.detections 是唯一输出通道；ctx.metadata["segments"] 已删除。
# ctx.metadata["segment_count"] 仍记录本步产出的段数。
```

字段对照：`SegmentResult.class_name` → `SegmentDetection.object_type`；`SegmentResult.mask`（全幅）→ `SegmentDetection.mask`（bbox 局部）；`class_id` 保留。

`MaskApplyStep` 已同步更新：它从 `ctx.detections` 里筛出 `RegionDetection` 载荷，读其 bbox 局部 mask，内部贴回全幅后再做填充+裁剪，行为对使用方不变。

### FilterStep `classes` 修复

之前 `FilterStep({"classes": [...]})` 会命中 `det.object_type.value`（把 str 当 enum）而抛 `AttributeError`。现已改为直接用 `object_type` 字符串，与 `NMSStep` 一致——之前因这个 bug 没法用 `classes` 的配置现在可以正常用了。

## 明确不做

- 抓取语义（`pick_point` / `eligible` 等）不进 autoweaver——那是项目侧的事
- 不做 YAML 字段 schema
- 不动 BT / Worker / comm
