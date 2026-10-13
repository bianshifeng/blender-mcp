# MetaOri 3D

MetaOri 3D 是本 fork 在 blender-mcp 服务端加入的模块：以本机 ComfyUI 为生产后台，通过 MCP 提供文生图、图生模型和模型规范化。设计见 `docs/superpowers/specs/2026-09-28-metaori-3d-design.md`。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `METAORI_COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `METAORI_OUTPUT_DIR` | `~/MetaOri/AI_Raw` | 任务目录根（每个任务一个子目录，永不覆盖） |
| `METAORI_EXPORT_DIR` | `~/MetaOri/Export` | 规范化后 FBX 的导出根目录 |

相对路径按 MCP 服务进程的工作目录解析，建议写绝对路径。以 fork 源码运行的 MCP 客户端配置：

```json
"blender": {
  "command": "uv",
  "args": ["run", "--frozen", "--project", "<fork 路径>", "mcp-for-blender"],
  "env": {"METAORI_OUTPUT_DIR": "<项目>/SourceArt/AI_Raw", "METAORI_EXPORT_DIR": "<项目>/SourceArt/Export"}
}
```

Blender 插件无需改动或重装：MetaOri 只通过插件已有的 `execute_code` 命令运行 `bpy_scripts/` 下的脚本。

## 工具

| 工具 | 作用 |
|---|---|
| `metaori_status` | ComfyUI 状态、工作流与参数、输出目录 |
| `metaori_generate_image` | 功能 1：文生图（`purpose`：reference / texture / concept / raw；concept 为 1664×928 人眼高度场景概念图，供 scene-kickoff 立项用，`asset_name` 填 Mod 名）；可带 `reference_images`（本地图片，最多 6 张）做参考图生图 / 改图 |
| `metaori_generate_model` | 功能 2：图生模型（`image_job_id` 或 `image_path`） |
| `metaori_get_job` | 查询或等待任务；完成时输出已下载到任务目录 |
| `metaori_cancel_job` | 取消排队或运行中的任务 |
| `metaori_import_model` | 把任务 GLB 或任意模型文件导入 Blender |
| `metaori_normalize_model` | 功能 3：规范化并按 UE 预设导出 FBX |

典型流程：

1. `metaori_status`：确认 ComfyUI 可达、工作流节点齐全。
2. `metaori_generate_image(prompt=..., asset_name="BronzeCenser")`，再 `metaori_get_job(job_id, wait_seconds=120)`，查看 `outputs.images`。
3. `metaori_generate_model(image_job_id=..., target_faces=80000, texture_size=2048)`，再多次 `metaori_get_job(job_id, wait_seconds=300)`。
4. `metaori_import_model(job_id=...)`，用 `get_viewport_screenshot` 判断朝向。
5. `metaori_normalize_model(object_name=<导入的根对象>, asset_name="BronzeCenser", target_size_m=0.35, rotate_deg=[0, 0, 0], target_tris=50000)`。

## 注意事项（2026-09-28 端到端验证）

- **截图前保持实体（Solid）着色**：Blender 5.1 中视口处于材质预览或渲染（EEVEE）模式时，`get_viewport_screenshot` 的离屏绘制会让 Blender 崩溃（`EXCEPTION_ACCESS_VIOLATION`，位于 `eevee::ShadowTileMap::sync_orthographic`）。要看贴图，用 Solid + `shading.color_type = "TEXTURE"`。
- **朝向**：Pixal3D 生成的模型导入后正面朝 -Y（与参考图视角一致），一般不需要 `rotate_deg`。
- **耗时**（RTX 4090）：文生图预热后约 11 秒；图生模型首次运行约 580 秒（含模型加载与显存换入）；导入与规范化（8 万面降到 3 万面）约 2 秒。

## 工作流包

| id | 能力 | 说明 |
|---|---|---|
| `t2i_qwen_image_2_1` | text_to_image（默认） | Qwen Image 2.1 int8 文生图，extras：`steps` |
| `t2i_qwen_image_2_1_ref` | text_to_image | 同一模型，带 6 个参考图槽和 3 个 LoRA 槽；传 `reference_images` 时自动选它。extras：`steps`、`reference_resolution`（参考图缩放边长，0 = 原尺寸）、`lora_1..3`（ComfyUI 列出的 LoRA 文件名）与 `lora_1..3_strength`（默认 0 = 空槽） |
| `i2m_pixal3d` | image_to_3d（默认） | Pixal3D / TRELLIS.2 图生模型 |

参考图的用法写在提示词里（“图 1 的大象，换成青铜锈色”“图 2 的纹样”）。2026-10-13 实测：不带参考图约 9 s，带 1 张参考图约 11 s，改材质时保留了参考图的造型与纹样。

## 任务目录

```
<METAORI_OUTPUT_DIR>/<AssetName>/<YYYYMMDD-HHMMSS>_<t2i|i2m>_<job8>/
    image_01.png ...        文生图输出
    reference_01.png ...    参考图生图时的参考图副本
    input.png, model.glb    图生模型的参考图副本与输出
    job.json
```

`job.json` 记录提示词（原文与最终版本）、实际生效的参数（含 seed）、被跳过的参数、工作流 id 与 sha256、ComfyUI prompt_id、状态、耗时和输出路径。

## 更换或新增工作流

1. 在 ComfyUI 中搭好工作流，用菜单 “导出（API）” 得到 API 格式 JSON。
2. 新建 `src/blender_mcp/metaori/workflows/<id>/`（目录名即 id），放入 `workflow_api.json`。
3. 写 `manifest.json`（参考现有的包）：
   - `capability`：`text_to_image` 或 `image_to_3d`；
   - `inputs`：标准参数 → 目标列表 `[{"node", "class_type", "input"}]`，一个参数可以绑多个节点；列表参数（`reference_images`）改为槽列表 `[{"set": 目标, "detach": [目标…]}]`：第 i 个值写进第 i 个槽的 `set`（通常是 LoadImage 的 `image`），没用到的槽删掉 `detach` 指向的连线（必须是连线，所在输入要是可选的），之后不再通向输出的节点会从提交的图里剔除；
   - `extras`：工作流特有参数（`type`、`default`、`min`/`max` 或枚举 `values`、`bind`），`default` 必须与图里的当前值一致；
   - `outputs`：标准输出 → `{"node", "class_type", "key"}`（图片保存节点用 `images`，3D 保存节点用 `3d`）；
   - 同一能力只能有一个 `"default": true`。
4. 重启 MCP 服务，用 `metaori_status` 检查：加载失败的包列在 `rejected_workflows` 并写明原因，缺失的节点类列在 `missing_node_classes`。
5. 运行 `uv run --frozen --with pytest pytest tests/test_metaori_workflows.py -q`；若替换了默认工作流，同步更新 `test_shipped_packs_load_as_the_defaults` 中的 id。

| 能力 | 标准输入（* 必须绑定） | 标准输出 |
|---|---|---|
| text_to_image | prompt*、negative_prompt、width、height、seed、count、reference_images（内部，槽列表）、filename_prefix（内部） | images |
| image_to_3d | image*（内部）、seed、target_faces、texture_size、remove_background、filename_prefix（内部） | model |

## 离线运行 bpy 脚本

```bash
blender -b scene.blend -P src/blender_mcp/metaori/bpy_scripts/normalize.py -- --params params.json
```

`params.json` 的字段与 `metaori_normalize_model` 发给 Blender 的相同（`object_name`、`asset_name`、`export_path` 等）。

## 测试

```bash
uv run --frozen --with pytest pytest -q tests/test_metaori_*.py
```

`test_metaori_bpy_integration.py` 会以后台模式启动 Blender（找不到时跳过，可用 `METAORI_BLENDER_EXE` 指定）。
