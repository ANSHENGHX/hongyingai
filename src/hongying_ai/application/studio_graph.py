from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph


class StudioGraphState(TypedDict, total=False):
    """
    定义 Studio 工作流（视频/媒体生成）的图状态结构。
    使用 total=False 表示所有字段在初始状态下都是可选的，
    各个节点可以根据执行进度逐步向状态中写入和更新数据。
    """

    # --- 基础请求与上下文信息 ---
    request: Any  # 原始请求对象
    tenant_id: int  # 租户 ID
    trace_id: str  # 链路追踪 ID，用于日志和监控
    run: Any  # 运行实例或上下文对象

    # --- 模板与素材信息 ---
    template: Any  # 使用的模板信息
    asset_list: list[Any]  # 初始资产/素材列表
    manifest: Any  # 资产清单或配置清单
    visual_assets: tuple[Any, ...]  # 视觉资产集合

    # --- 路由控制标志 ---
    material_route: Literal[
        "uploaded", "generate"
    ]  # 素材路由：使用已上传素材 ("uploaded") 或 生成新素材 ("generate")
    scene_route: Literal[
        "static", "dynamic"
    ]  # 场景路由：使用静态图片场景 ("static") 或 生成动态视频场景 ("dynamic")

    # --- 生成结果与资产 ID ---
    generated_image_asset_ids: tuple[str, ...]  # 生成的图片资产 ID 列表
    generated_video_asset_ids: tuple[str, ...]  # 生成的视频资产 ID 列表
    avatar_agent: dict[str, Any]  # 人物口播智能体的身份锁定与脚本策略
    narration_asset_id: str | None  # 旁白/配音资产 ID
    effective_bgm_asset_id: str | None  # 最终使用的背景音乐 (BGM) 资产 ID

    # --- 警告与异常信息 ---
    media_generation_warning: str | None  # 媒体生成过程中的警告信息
    voiceover_warning: str | None  # 配音生成过程中的警告信息

    # --- 计划、故事板与时间线 ---
    snapshot: Any  # 状态快照
    brief: Any  # 内容摘要/简介
    storyboard: Any  # 故事板数据
    base_timeline: Any  # 基础时间线数据
    timeline: Any  # 最终时间线数据
    plan_key: str  # 计划唯一标识键
    model_meta: dict[str, Any]  # 模型元数据配置


# 定义图节点函数的类型别名：
# 接收当前状态 (StudioGraphState) 和运行配置 (RunnableConfig)，
# 返回一个包含状态更新字典的异步协程。
GraphNode = Callable[[StudioGraphState, RunnableConfig], Awaitable[dict[str, Any]]]


def build_studio_graph(
    *,
    prepare: GraphNode,
    route_materials: GraphNode,
    match_uploaded: GraphNode,
    prepare_avatar_pitch: GraphNode,
    generate_images: GraphNode,
    use_static_scenes: GraphNode,
    generate_dynamic_scenes: GraphNode,
    generate_voiceover: GraphNode,
    plan: GraphNode,
    build_timeline: GraphNode,
    validate_timeline: GraphNode,
    persist_plan: GraphNode,
    compose_and_quality: GraphNode,
) -> Any:
    """
    构建并编译 Studio 媒体生成工作流的 LangGraph 状态图。

    该图定义了从输入验证、素材路由、场景生成、配音、故事板规划、
    时间线构建到最终视频合成与质量检查的完整流水线。

    Args:
        prepare: 输入验证与初始化准备节点。
        route_materials: 素材路由节点，决定使用上传素材还是生成新素材。
        match_uploaded: 匹配已上传素材节点。
        prepare_avatar_pitch: 校验人物参考图并准备口播镜头策略。
        generate_images: 生成场景图片节点。
        use_static_scenes: 使用静态图片序列作为场景节点。
        generate_dynamic_scenes: 生成动态视频场景节点。
        generate_voiceover: 生成配音/旁白节点。
        plan: 规划摘要、故事板和计划的节点。
        build_timeline: 构建时间线节点。
        validate_timeline: 验证时间线 Schema 节点。
        persist_plan: 持久化计划数据节点。
        compose_and_quality: 使用 FFmpeg 合成视频并进行质量检查节点。

    Returns:
        编译后的 LangGraph CompiledGraph 对象。
    """
    # 初始化状态图构建器
    builder = StateGraph(StudioGraphState)

    # ================= 1. 注册图节点 =================
    # 将传入的节点函数注册到图中，并赋予具有业务含义的节点名称
    builder.add_node("validate_input", prepare)  # 验证输入并初始化状态
    builder.add_node("route_materials", route_materials)  # 决定素材来源路由
    builder.add_node("match_uploaded_materials", match_uploaded)  # 匹配用户上传的素材
    builder.add_node("avatar_spokesperson_agent", prepare_avatar_pitch)  # 人物口播智能体
    builder.add_node("generate_scene_images", generate_images)  # 调用模型生成场景图片
    builder.add_node("use_static_scene_sequence", use_static_scenes)  # 处理静态图片场景序列
    builder.add_node("generate_dynamic_scene_videos", generate_dynamic_scenes)  # 处理动态视频场景生成
    builder.add_node("generate_voiceover", generate_voiceover)  # 生成语音旁白
    builder.add_node("planner_brief_storyboard", plan)  # 生成摘要、故事板与整体计划
    builder.add_node("build_timeline", build_timeline)  # 根据故事板构建详细时间线
    builder.add_node("validate_timeline_schema", validate_timeline)  # 校验时间线数据结构的合法性
    builder.add_node("persist_plan", persist_plan)  # 将最终计划与时间线持久化存储
    builder.add_node("composer_ffmpeg_quality", compose_and_quality)  # 调用 FFmpeg 合成最终视频并质检

    # ================= 2. 定义图的边（流转逻辑） =================

    # 2.1 入口与初始化阶段
    builder.add_edge(START, "validate_input")
    builder.add_edge("validate_input", "route_materials")

    # 2.2 素材路由分支
    # 根据 material_route 的值决定下一步：
    # - "uploaded": 走匹配已上传素材的逻辑
    # - "generate": 走 AI 生成场景图片的逻辑
    builder.add_conditional_edges(
        "route_materials",
        lambda state: state["material_route"],
        {
            "uploaded": "match_uploaded_materials",
            "generate": "generate_scene_images",
        },
    )

    # 2.3 静态/动态场景路由分支
    # 如果走生成图片逻辑，生成后根据 scene_route 决定场景类型：
    # - "static": 使用静态图片序列
    # - "dynamic": 将图片转为动态视频
    builder.add_conditional_edges(
        "generate_scene_images",
        lambda state: state["scene_route"],
        {
            "static": "use_static_scene_sequence",
            "dynamic": "generate_dynamic_scene_videos",
        },
    )

    # 2.4 汇聚到配音节点
    # 无论是匹配上传素材、使用静态场景还是生成动态场景，最终都汇聚到配音节点
    builder.add_conditional_edges(
        "match_uploaded_materials",
        lambda state: (
            "avatar"
            if state["request"].options.generation_direction == "avatar_product_pitch"
            else (
                "dynamic"
                if state["scene_route"] == "dynamic"
                and any(getattr(asset, "media_type", None) == "image" for asset in state["visual_assets"])
                else "direct"
            )
        ),
        {
            "avatar": "avatar_spokesperson_agent",
            "dynamic": "generate_dynamic_scene_videos",
            "direct": "generate_voiceover",
        },
    )
    builder.add_edge("avatar_spokesperson_agent", "generate_dynamic_scene_videos")
    builder.add_edge("use_static_scene_sequence", "generate_voiceover")
    builder.add_edge("generate_dynamic_scene_videos", "generate_voiceover")

    # 2.5 后期制作流水线（线性流转）
    # 配音 -> 规划故事板 -> 构建时间线 -> 校验时间线 -> 持久化 -> 视频合成与质检 -> 结束
    builder.add_edge("generate_voiceover", "planner_brief_storyboard")
    builder.add_edge("planner_brief_storyboard", "build_timeline")
    builder.add_edge("build_timeline", "validate_timeline_schema")
    builder.add_edge("validate_timeline_schema", "persist_plan")
    builder.add_edge("persist_plan", "composer_ffmpeg_quality")
    builder.add_edge("composer_ffmpeg_quality", END)

    # 编译并返回可执行的图实例
    return builder.compile()
