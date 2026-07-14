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

---

# 0.11.0 → 0.11.1：`run_async` 异常不再吞掉 on_done

## 病因

0.11.0 及之前，`Worker.run_async(fn, on_done)` 的后台 job 一旦抛异常，`AsyncPool.submit` 会 catch `BaseException`、打一行 `"on_done suppressed"` 日志，然后**永不排任何回调**。后果：靠 `on_done` 写 `last_completed_id` 完成 BT 请求的 Worker（典型是 `MotionWorker` 派生类），在 job 失败时请求**永远挂着**——`NotifyAndWait` 死等一个永不到来的完成，整棵 BT hang。这直接违反 EVO-007 的公理"错误也必须完成请求以防 BT hang"（见 `note_error` 语义）。历史上消费方（hub 的 `PlcArmWorker`）被迫自己写一层 `_run_async_guarded` 哨兵包装来补这个洞。

## 新语义（行为变化）

`run_async` / `AsyncPool.submit` 新增可选 `on_error` 回调：

```python
self.run_async(fn, on_done, on_error)      # on_error: Callable[[BaseException], None]
```

- **每个 job 恰好排一个回调**：`fn` 正常返回 → `on_done(result)`；`fn` 抛异常 → `on_error(exc)`。
- 两条路径都在**下一 tick 主线程**的 `drain_main_thread_callbacks` 里触发，所以 `on_error` 里做完成记账（写 `last_error` / `last_completed_id`、改 self）依旧是 tick-safe 的。
- **异常不再被静默吞掉**：若 `fn` 抛异常且没给 `on_error`，打印完整 traceback、不排回调（不再有 `"suppressed"` 措辞）。
- **正常路径完全不变**：`on_done` 签名、时序、只在成功时收到 result——一字未改。老代码（只传 `on_done`）继续按原样编译运行。

## 迁移步骤

凡是用 `run_async` 的 `on_done` 驱动 BT 请求完成的 Worker，把失败兜底交给框架的 `on_error`，删掉自建哨兵：

```python
# 旧（0.11.0，消费方自己补洞）
def _run_async_guarded(self, job, done, on_error):
    _SENTINEL = object()
    def wrapped_job():
        try: return job()
        except BaseException as exc: return (_SENTINEL, exc)
    def wrapped_done(result):
        if isinstance(result, tuple) and result[0] is _SENTINEL:
            on_error(result[1]); return
        done(result)
    self.run_async(wrapped_job, wrapped_done)

# 新（0.11.1）
self.run_async(job, done, on_error)
```

纯 perception / fire-and-forget 用法（不传 `on_error`、不靠 `on_done` 记完成）无需改动。
