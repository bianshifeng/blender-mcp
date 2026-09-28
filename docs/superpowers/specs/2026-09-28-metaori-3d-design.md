# MetaOri 3D 设计文档

- 日期：2026-09-28
- 状态：已确认（用户授权 Claude 全权负责设计与维护）
- 仓库：`bianshifeng/blender-mcp`（`mcp-for-blender` 的 fork），分支 `feat/metaori-3d`
- 使用者：Claude（通过 MCP）。MetaOri 是 Claude 在 UE_VR_Game 项目中生产 AI 3D 资产的工具，由 Claude 持续维护和扩展。

## 1. 目标与范围

MetaOri 3D 是 blender-mcp 服务端里的一个模块，把本机 ComfyUI（`http://127.0.0.1:8188`）当作生产后台，提供三项功能：

1. **文生图**：输入提示词，生成模型参考图（给图生模型用）或贴图。
2. **图生模型**：输入参考图，生成带 PBR 贴图的 GLB 模型。
3. **模型规范化**：对 Blender 中指定的模型做变换、清理、面数预算、命名，并按 UE 预设导出 FBX。

要求：接口标准化（输入输出固定），ComfyUI 工作流可替换且不改代码；生成过程不阻塞 Blender；每个产出都可追溯（提示词、参数、seed、工作流版本）。

不在范围内（YAGNI）：Blender 面板 UI、WebSocket 实时进度、UE 端自动导入、LOD 与 UCX 碰撞生成、无缝贴图保证、多视图生成。

## 2. 架构

```
Claude ──MCP(stdio)──> blender-mcp 服务端进程（本 fork）
                        ├─ server.py            上游代码；只加注册 MetaOri 的 2 行 + 说明 1 行
                        └─ blender_mcp/metaori/ 新包
                            ├─ config.py        环境变量配置
                            ├─ capabilities.py  能力契约（标准输入/输出、校验）+ purpose 预设
                            ├─ presets.json     提示词预设（reference / texture / raw）
                            ├─ workflows.py     工作流包加载、校验、参数绑定
                            ├─ workflows/<id>/  manifest.json + workflow_api.json
                            ├─ comfy_client.py  ComfyUI HTTP 客户端（httpx 异步）
                            ├─ jobs.py          任务记录与输出目录（永不覆盖）
                            ├─ service.py       编排：提交 / 查询 / 下载 / 取消
                            ├─ blender_ops.py   经现有 execute_code 通道运行 bpy 脚本
                            ├─ bpy_scripts/     import_model.py、normalize.py
                            └─ tools.py         7 个 MCP 工具（薄封装）
ComfyUI :8188  <── HTTP，服务端直连，不经过 Blender
Blender 插件   <── 只接收导入、规范化两步（execute_code）；插件不改、不重装
```

设计要点：

- **生成是异步任务**：提交时由 MetaOri 生成 UUID 作为 `prompt_id`（ComfyUI 0.37 支持客户端指定），即 `job_id`；提交后立即返回，之后用 `metaori_get_job` 查询或等待。
- **与上游隔离**：新代码都在 `metaori` 包内；`server.py` 改 3 行；`addon.py` 不改。合并上游更新时冲突最小。
- **bpy 脚本是独立文件**：每个脚本定义 `run(params) -> dict`，服务端把脚本源码加上一段运行器经 `execute_code` 发给 Blender；同一脚本也能用 `blender -b -P` 离线运行（用于集成测试和批处理）。
- **单元可独立测试**：配置、契约、工作流、客户端、任务、编排都不依赖 bpy 或真实 ComfyUI。

## 3. 标准接口

### 3.1 能力契约

| 能力 | 标准输入 | 标准输出 |
|---|---|---|
| `text_to_image` | `prompt`（必填，非空，≤4000 字符）；`negative_prompt`；`width`/`height`（256–2048，8 的倍数）；`seed`（0 ≤ seed < 2^53）；`count`（1–4） | `images`：PNG 路径列表 |
| `image_to_3d` | `image`（必填，服务端上传后填入）；`seed`；`target_faces`（1000–2000000）；`texture_size`（512/1024/2048/4096）；`remove_background`（bool） | `model`：GLB 路径 |

两个能力都有内部参数 `filename_prefix`，由服务端设置为 `metaori/<AssetName>/<kind>_<job8>`，让 ComfyUI 自己的 output 目录也按资产归档。

参数规则：

- 调用方显式传入的标准参数，如果所选工作流没有绑定，报错（例如某工作流不支持 `target_faces`）。
- 服务端注入的默认值（随机 `seed`、`filename_prefix`、purpose 预设的宽高），若工作流没有绑定则跳过，并在 `job.json` 中记录为未生效。
- 未传的可选参数沿用工作流里的原值（例如 Pixal3D 工作流的 70 万面、4096 贴图）。

### 3.2 purpose 预设（`presets.json`）

仅用于 `text_to_image`：

- `reference`（默认）：正向追加“单个物体、居中、完整、3/4 正面视角、纯白背景、柔和均匀光、无投影”等英文短语；负向追加“多个物体、被裁切、杂乱背景、文字、水印、强阴影”；默认 1024×1024。
- `texture`：正向追加“无缝平铺、正交俯视、均匀漫射光、无阴影、无透视”；负向追加“透视、物体、阴影、暗角、边框、文字”；默认 1024×1024。当前工作流不保证真正无缝。
- `raw`：提示词原样使用，宽高沿用工作流。

组合规则：`positive = prompt + ", " + suffix`；`negative = 预设负向 + 用户负向`（以逗号连接，忽略空项）；两者都为空时不改动工作流里的负向提示词。

### 3.3 MCP 工具

所有工具返回 JSON 字符串（`ensure_ascii=False`）；失败时抛出异常，由 FastMCP 转为错误结果。

| 工具 | 参数 | 返回 |
|---|---|---|
| `metaori_status()` | 无 | ComfyUI 地址、是否可达、版本、GPU 与显存、队列长度；各工作流的 id、能力、是否默认、参数说明、缺失的节点类；输出和导出目录的绝对路径 |
| `metaori_generate_image(prompt, asset_name, purpose="reference", negative_prompt=None, width=None, height=None, seed=None, count=None, workflow=None, params=None, wait_seconds=0)` | 见 3.1 | 任务记录 |
| `metaori_generate_model(asset_name=None, image_path=None, image_job_id=None, image_index=0, seed=None, target_faces=None, texture_size=None, remove_background=None, workflow=None, params=None, wait_seconds=0)` | `image_path` 与 `image_job_id` 二选一；从任务取图时 `asset_name` 默认沿用源任务 | 任务记录 |
| `metaori_get_job(job_id, wait_seconds=60)` | `wait_seconds` 0–600 | 任务记录；成功时包含本地输出路径 |
| `metaori_cancel_job(job_id)` | | 任务记录 |
| `metaori_import_model(job_id=None, file_path=None, name=None)` | 二选一；支持 .glb/.gltf/.fbx/.obj | 新建对象、网格对象、合并后的世界包围盒、三角面数、材质、贴图 |
| `metaori_normalize_model(object_name, asset_name, ...)` | 见第 6 节 | 规范化报告 |

两个生成工具的 `wait_seconds` 与 `metaori_get_job` 相同：提交后在该时间内等待终态，超时则返回当前状态。

`asset_name` 校验：`^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$`，最长 64 字符（与项目 PascalCase 规范一致，不含空格和中文）。

## 4. 工作流包

### 4.1 目录与 manifest

```
src/blender_mcp/metaori/workflows/<id>/
    manifest.json        # 绑定声明
    workflow_api.json    # 从 ComfyUI “导出（API）”原样保存
```

manifest（schema `metaori.workflow/1`）：

```json
{
  "schema": "metaori.workflow/1",
  "id": "i2m_pixal3d",
  "capability": "image_to_3d",
  "default": true,
  "description": "...",
  "graph": "workflow_api.json",
  "inputs": {
    "<标准参数>": [{"node": "122", "class_type": "LoadImage", "input": "image"}]
  },
  "extras": {
    "<额外参数>": {
      "type": "int|float|bool|str|enum",
      "description": "...",
      "default": "...",
      "min": 0, "max": 1,
      "values": {"<枚举名>": "<写入的值>"},
      "bind": [{"node": "316", "class_type": "PrimitiveBoolean", "input": "value"}]
    }
  },
  "outputs": {
    "<标准输出>": {"node": "322", "class_type": "Save3DAdvanced", "key": "3d"}
  }
}
```

### 4.2 加载校验（任一失败则该工作流不可用，并在 `metaori_status` 中报告原因）

- schema 版本、能力名合法；`id` 与目录名一致；同一能力最多一个 `default`。
- 每个绑定目标：节点存在、`class_type` 一致、`input` 字段在节点 inputs 中存在。
- 能力的必填输入已绑定（t2i：`prompt`；i2m：`image`）；必需输出已声明（t2i：`images`；i2m：`model`）。
- `inputs` 里只能出现该能力的标准参数；`extras` 不能与标准参数重名；`extras.default` 符合类型。
- 与 ComfyUI `/object_info` 对照，列出工作流里未安装的节点类（启动或 `metaori_status` 时检查，结果缓存 5 分钟）。

### 4.3 绑定

深拷贝原图，把每个参数值写入所有绑定目标的 `inputs[input]`；如果原值是连线（`[node_id, index]`），直接替换为常量（例如 Qwen 的宽高替换来自 `ResolutionSelector` 的连线，未被引用的节点由 ComfyUI 自动跳过）。枚举型 extras 先按 `values` 映射。未知的 `params` 名称报错。

### 4.4 初始工作流包

`t2i_qwen_image_2_1`（`text_to_image`，默认）：

| 参数 | 目标 |
|---|---|
| `prompt` | 502 `CR Prompt Text`.prompt |
| `negative_prompt` | 452 `TextEncodeQwenImage21`.negative_prompt |
| `width` / `height` / `count` | 456 `EmptyLatentImage`.width / height / batch_size |
| `seed` | 458 `KSampler`.seed |
| `filename_prefix` | 461 `SaveImageAdvanced`.filename_prefix |
| extras `steps` | 458 `KSampler`.steps（int，1–100） |
| 输出 `images` | 461 的 `images` |

`i2m_pixal3d`（`image_to_3d`，默认）：

| 参数 | 目标 |
|---|---|
| `image` | 122 `LoadImage`.image |
| `seed` | 3、12、18、23 `KSampler`.seed |
| `target_faces` | 186 `DecimateMesh`.target_face_count |
| `texture_size` | 288 `PrimitiveInt`.value |
| `remove_background` | 248 `ComfySwitchNode`.switch |
| `filename_prefix` | 322 `Save3DAdvanced`.filename_prefix |
| extras `model_variant` | 316 `PrimitiveBoolean`.value（`pixal3d`→false，`trellis2`→true，默认 `pixal3d`） |
| extras `remesh_resolution` | 241 `RemeshMesh`.resolution（int，256–2048） |
| 输出 `model` | 322 的 `3d` |

## 5. 任务模型

### 5.1 状态

`queued`（在 `queue_pending` 中，附排队位置）→ `running`（在 `queue_running` 中）→ `succeeded` / `failed` / `cancelled`。

- 历史记录 `status_str == "success"`：下载全部声明的输出后记为 `succeeded`；声明的输出缺失记为 `failed`。
- `status_str == "error"`：消息里有 `execution_interrupted` 记为 `cancelled`，否则 `failed`，并附 `execution_error` 的节点 id、节点类型、异常信息。
- 既不在队列也不在历史里（ComfyUI 重启或历史被清）：`failed`，原因 `lost`。
- 取消：排队中用 `POST /queue {"delete": [id]}` 并记为 `cancelled`；运行中用 `POST /interrupt {"prompt_id": id}`，之后由历史记录确认。

`get_job` 在 `wait_seconds` 内每 2 秒轮询一次，进入终态立即返回。终态任务再次查询时直接读记录，不再访问 ComfyUI。

### 5.2 目录与 job.json

```
<METAORI_OUTPUT_DIR>/<AssetName>/<YYYYMMDD-HHMMSS>_<t2i|i2m>_<job8>/
    image_01.png ...        # t2i 输出
    input.<ext>             # i2m 使用的参考图副本
    model.glb               # i2m 输出
    job.json
```

- 任务目录在提交时创建，已存在则报错（永不覆盖）。
- 下载先写 `.part` 再原子替换。
- `job.json`（schema `metaori.job/1`）：`job_id`、`capability`、`workflow{id, sha256}`、`asset_name`、`purpose`、`prompt{user, positive, negative}`、`source{image_path, image_job_id, image_index, uploaded_as}`、`params`（实际生效的标准参数，含 seed）、`skipped_params`、`extras`、`comfyui{url, prompt_id}`、`status`、`queue_position`、`error`、`created_at`、`finished_at`、`duration_s`、`outputs`（标准输出名 → 绝对路径或路径列表）。
- 按 `job_id` 查找：先查内存索引，未命中时扫描 `<输出目录>/*/*_<job8>/job.json` 并核对完整 id，服务重启后也能找回。

### 5.3 ComfyUI 客户端

- `GET /system_stats`、`GET /object_info`
- `POST /upload/image`（`type=input`、`subfolder=metaori`、`overwrite=true`；文件名为内容 sha256 前 16 位加原扩展名）→ `LoadImage` 取值 `metaori/<name>`
- `POST /prompt {"prompt", "prompt_id", "client_id"}`；HTTP 400 时解析 `error` 与 `node_errors`
- `GET /history/{id}`、`GET /queue`、`GET /view`（流式下载）、`POST /queue`、`POST /interrupt`
- 连接失败统一报 `ComfyUnavailable`，提示启动 ComfyUI。

## 6. Blender 侧

### 6.1 传输

`blender_ops.run_script(blender, name, params)`：读取 `bpy_scripts/<name>.py`，追加运行器：用 `repr(json.dumps(params))` 嵌入参数；调用 `run(params)`；成功打印 `METAORI_RESULT:<json>`，异常时打印 `METAORI_ERROR:<json>`（类型、消息、traceback），不向外抛，以免服务端重置套接字。服务端取 stdout 中最后一个标记行解析；没有标记行时连同原始输出一起报错。

### 6.2 import_model

参数：`file_path`、`name`。按扩展名调用 glTF（`merge_vertices=True`；若算子支持则 `import_scene_as_collection=False`）、FBX 或 OBJ 导入；对比导入前后的 `bpy.data.objects` 找出新对象；只有一个根对象时按 `name` 重命名。返回新对象列表、网格对象、合并后的世界包围盒（米）、三角面数、材质、贴图（名称和尺寸）。

### 6.3 normalize 流水线

参数与默认值：

| 参数 | 默认 | 说明 |
|---|---|---|
| `object_name` | 必填 | 网格对象，或下面挂有网格对象的空物体 |
| `asset_name` | 必填 | 用于命名与导出路径 |
| `apply_modifiers` | true | 先把修改器结果烘进网格 |
| `rotate_deg` | null | 预旋转 [x, y, z]（度），用于把模型正面转到 -Y |
| `target_size_m` | null | 目标尺寸（米）；null 表示不缩放 |
| `size_axis` | `height` | `height`(Z) / `width`(X) / `depth`(Y) / `longest` |
| `merge_distance_m` | 0.0001 | 合并重复顶点（同时焊接 glTF 在 UV 缝处拆开的顶点） |
| `min_island_ratio` | 0.01 | 删除面数小于最大块该比例的浮块；0 表示关闭 |
| `recalc_normals` | true | 法线统一朝外 |
| `target_tris` | null | 三角面预算；超出时用 Decimate（Collapse）降面 |
| `smooth_angle_deg` | 30 | 清除自定义法线后按角度平滑；null 表示保留原法线 |
| `rename` | true | 对象和网格命名为 `SM_<AssetName>`，材质为 `MI_<AssetName>`（多个时加 `_01`…） |
| `export_fbx` | true | 导出到 `<METAORI_EXPORT_DIR>/<AssetName>/SM_<AssetName>.fbx` |
| `overwrite_export` | false | 目标 FBX 已存在时，只有为 true 才覆盖 |

执行顺序：检查（骨骼蒙皮对象报错；`SM_<AssetName>` 已被其他对象占用时报错）→ 收集网格对象 → 各自应用修改器（必须在合并前，否则非活动对象的修改器会丢失）→ 解除父子关系并保持世界变换 → 多个网格合并为一个 → 预旋转 → 把世界变换烘进网格 → 缩放到目标尺寸 → 合并重复顶点、删除游离点和边 → 删除小浮块 → 重算法线 → 降面 → 平滑 → 枢轴移到底部中心、对象移到世界原点 → 命名 → 导出 FBX。

导出预设 `ue`（与项目 `Tools/blender/export_fbx.py` 一致）：`object_types={'MESH'}`、`global_scale=1.0`、`apply_unit_scale=True`、`apply_scale_options='FBX_SCALE_NONE'`、`axis_forward='-Z'`、`axis_up='Y'`、`mesh_smooth_type='FACE'`、`use_mesh_modifiers=True`、`add_leaf_bones=False`、`bake_anim=False`。导出时只选中目标对象，完成后恢复原选择。

报告：`before` / `after` 统计（尺寸（米）、三角面、顶点、非流形边、UV 层、材质、位置；`after` 另含连通块数）、每一步的执行记录（合并顶点数、删除的浮块数与面数、缩放系数、降面比例等）、导出路径与文件大小、警告（例如场景单位缩放不为 1）。

## 7. 配置与接入

环境变量（相对路径按服务进程工作目录解析；`metaori_status` 返回解析后的绝对路径）：

| 变量 | 默认 |
|---|---|
| `METAORI_COMFYUI_URL` | `http://127.0.0.1:8188` |
| `METAORI_OUTPUT_DIR` | `~/MetaOri/AI_Raw` |
| `METAORI_EXPORT_DIR` | `~/MetaOri/Export` |

UE_VR_Game 项目接入：

- `.mcp.json` 的 `blender` 服务器改为 `uv run --frozen --project <仓库>/Tools/blender-mcp mcp-for-blender`，环境变量指向 `SourceArt/AI_Raw` 与 `SourceArt/Export`（使用绝对路径，不依赖启动目录）。服务器名保持 `blender`，工具名前缀不变。
- `pyproject.toml` 的 package-data 加入 `metaori/presets.json`、`metaori/workflows/*/*.json`、`metaori/bpy_scripts/*.py`。
- `SERVER_INSTRUCTIONS` 在生成器来源列表中加一行 MetaOri（总长度保持 < 2500）。
- 文档：fork 内 `docs/metaori.md`（使用方法、新增工作流包步骤）；项目 CLAUDE.md（MCP 表、用法要点、B 级流程、变更记录）、`Docs/progress.md`、`Docs/setup_mcp.md`。

## 8. 错误处理

| 情况 | 处理 |
|---|---|
| ComfyUI 不可达 | `ComfyUnavailable`，附地址与启动提示 |
| 参数不合法、工作流不支持该参数、未知 `params` | 提交前报错，不创建任务目录 |
| ComfyUI 校验失败（HTTP 400） | 报告 `error` 与各节点 `node_errors`（节点 id、类型、原因），任务记为 `failed` |
| 执行出错 | `failed`，附出错节点与异常信息 |
| 输出缺失、下载失败 | `failed`；已下载的文件保留 |
| 找不到 job_id | 报错，提示用 `metaori_status` 查看输出目录 |
| Blender 未连接或脚本出错 | 报告异常类型、消息和 traceback；场景可能已部分修改，报告中说明 |
| 导出目标已存在 | 未设 `overwrite_export` 时报错，网格改动保留 |

## 9. 测试

- 单元测试（pytest，不依赖网络和 Blender）：
  - 配置解析；能力契约与 purpose 组合；`asset_name` 校验。
  - 工作流：加载两个真实包，各类错误 manifest，绑定（多目标、替换连线、枚举映射、未知参数、不支持的参数），缺失节点类检测。
  - ComfyUI 客户端：用 `httpx.MockTransport` 覆盖上传、提交成功与 400、历史、队列、下载、取消。
  - 任务与编排：假客户端驱动的状态流转、下载、失败、丢失、取消、从图片任务串联图生模型、目录永不覆盖、重启后按 id 找回。
  - Blender 通道：假连接返回的 `METAORI_RESULT` / `METAORI_ERROR` 解析；参数中的 Windows 路径与中文往返。
  - 工具：注册到 FastMCP、参数透传、JSON 输出。
- 集成测试（找不到 Blender 时跳过；可用 `METAORI_BLENDER_EXE` 指定）：`blender -b --factory-startup` 运行测试脚本，造一个带重复顶点、浮块、旋转和缩放的测试网格并导出 GLB，再依次跑 `import_model` 与 `normalize`，检查尺寸、枢轴、浮块、面数、命名与 FBX。
- 端到端（手动一次）：通过 stdio MCP 客户端启动 fork 服务端，对真实 ComfyUI 与 Blender 跑“文生图 → 图生模型 → 导入 → 规范化 → 导出”，输出写到临时目录，记录耗时与结果。
- 回归：fork 原有测试全部通过（基线 269 通过、1 跳过）。

## 10. 已知限制与后续方向

- 规范化在 Blender 主线程执行，70 万面级别的网格会让界面卡顿数秒。
- ComfyUI 冷启动需要加载模型，首个任务可能需要数分钟。
- 图生模型的朝向需在端到端测试中确认，再在工具说明中写明常用的 `rotate_deg`。
- 后续可加：无缝贴图工作流包、多视图参考图、贴图单独导出与 UE 命名、LOD、UCX 碰撞、WebSocket 进度。
