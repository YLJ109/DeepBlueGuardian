"""连江县海洋生态监测区域与点位基础数据。

坐标与区域属性来自公开权威资料（福州市人民政府、福建省人民政府、福建省林业局、
农业农村部国家级海洋牧场公示、福建省海洋预报台），用于初始化区域、设备与
三维沙盘坐标。所有点位均为真实海域位置，经纬度取公开资料描述位置的中心点，
可按实际投放坐标在管理后台中微调。

三维沙盘坐标映射规则：
    world_x = (longitude - 119.85) * 43.333   # 经度 119.55—120.15 映射到 -13—13
    world_z = (26.37 - latitude) * 65.385     # 纬度 26.24—26.50 映射到 8.5—-8.5
前端沙盘、后端接口与文档共用同一映射，保证大屏点位与真实地理位置一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 坐标映射
# ---------------------------------------------------------------------------
LON_ORIGIN = 119.85
LAT_ORIGIN = 26.37
LON_SCALE = 43.333
LAT_SCALE = 65.385


def to_world(longitude: float, latitude: float) -> tuple[float, float]:
    """经纬度 -> 三维沙盘世界坐标（x 向东为正，z 向南为正）。"""

    return (
        round((longitude - LON_ORIGIN) * LON_SCALE, 3),
        round((LAT_ORIGIN - latitude) * LAT_SCALE, 3),
    )


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SiteSpec:
    """单个监测站点。"""

    code: str
    name: str
    kind: str  # farm / mangrove / estuary / open_sea / nursery
    longitude: float
    latitude: float
    region_code: str
    depth_m: float
    summary: str
    # 环境基线（用于仿真器给出的物理合理值域）
    temp_base: float
    temp_amp: float
    salinity_base: float
    salinity_amp: float
    do_base: float
    turbidity_base: float
    turbidity_amp: float
    chl_base: float
    nutrient_index: float  # 0—1，越高越容易发生藻华
    devices: tuple[str, ...] = field(default=("buoy",))

    @property
    def world(self) -> tuple[float, float]:
        return to_world(self.longitude, self.latitude)


@dataclass(frozen=True)
class RegionSpec:
    """监测区域（片区）。"""

    code: str
    name: str
    kind: str
    longitude: float
    latitude: float
    parent_code: str | None
    remark: str

    @property
    def world(self) -> tuple[float, float]:
        return to_world(self.longitude, self.latitude)


# ---------------------------------------------------------------------------
# 区域（依据公开资料的区位描述）
# ---------------------------------------------------------------------------
REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec(
        code="LJ-HQ",
        name="黄岐半岛海域",
        kind="farm",
        longitude=119.9750,
        latitude=26.3280,
        parent_code=None,
        remark="黄岐半岛位于连江东部突出部，是连江外海养殖与外海赤潮监测的前沿区域。",
    ),
    RegionSpec(
        code="LJ-DH",
        name="定海湾",
        kind="farm",
        longitude=119.8580,
        latitude=26.2900,
        parent_code=None,
        remark="隶属筱埕镇，连江三湾之一，闽江口北岸，海域面积约 135 km²，深远海养殖平台集中投放区。",
    ),
    RegionSpec(
        code="LJ-TL",
        name="苔菉北茭海域",
        kind="open_sea",
        longitude=120.0250,
        latitude=26.3620,
        parent_code=None,
        remark="连江最东端的北茭半岛海域，外海交换能力强，是赤潮自外海输入的前哨监测点。",
    ),
    RegionSpec(
        code="LJ-DLD",
        name="东洛岛深远海养殖区",
        kind="open_sea",
        longitude=120.0950,
        latitude=26.3620,
        parent_code=None,
        remark="东洛岛周边深远海养殖平台在建与投产区，规划养殖水体约 17 万立方米。",
    ),
    RegionSpec(
        code="LJ-MB",
        name="马鼻镇浮曦村红树林",
        kind="mangrove",
        longitude=119.7220,
        latitude=26.4180,
        parent_code=None,
        remark="罗源湾腹部，马鼻镇滩涂面积约 7.5 万亩，为福州市滩涂面积最大的乡镇，红树林核心观赏区之一。",
    ),
    RegionSpec(
        code="LJ-PK",
        name="浦口镇松坞村敖江口红树林",
        kind="mangrove",
        longitude=119.6120,
        latitude=26.2760,
        parent_code=None,
        remark="敖江入海口，福州红树林三大核心片区之一，受径流与潮汐双重影响。",
    ),
    RegionSpec(
        code="LJ-LYW",
        name="罗源湾鉴江—松山红树林集中区",
        kind="mangrove",
        longitude=119.7480,
        latitude=26.4650,
        parent_code="LJ-MB",
        remark="福建省面积最大、保存最完好的红树林集中区之一，约 320 公顷，以秋茄、桐花树、白骨壤、木榄为主。",
    ),
)

# ---------------------------------------------------------------------------
# 监测站点
# ---------------------------------------------------------------------------
SITES: tuple[SiteSpec, ...] = (
    SiteSpec(
        code="LJ-HQ-B01",
        name="黄湾屿海洋牧场浮标",
        kind="farm",
        longitude=119.9630,
        latitude=26.3050,
        region_code="LJ-HQ",
        depth_m=8.0,
        summary="黄岐半岛黄湾屿南侧海域，国家级海洋牧场核心区，总面积 258 公顷，已建人工鱼礁 36 公顷。",
        temp_base=20.6,
        temp_amp=7.4,
        salinity_base=30.2,
        salinity_amp=1.4,
        do_base=7.9,
        turbidity_base=6.5,
        turbidity_amp=3.0,
        chl_base=2.4,
        nutrient_index=0.52,
        devices=("buoy", "camera", "gateway"),
    ),
    SiteSpec(
        code="LJ-HQ-B02",
        name="黄岐湾中部浮标",
        kind="farm",
        longitude=119.9750,
        latitude=26.3280,
        region_code="LJ-HQ",
        depth_m=12.0,
        summary="黄岐湾中部开阔水域，海带与鲍鱼养殖集中区，赤潮监测加密点位。",
        temp_base=20.4,
        temp_amp=7.2,
        salinity_base=30.8,
        salinity_amp=1.2,
        do_base=8.1,
        turbidity_base=5.8,
        turbidity_amp=2.6,
        chl_base=2.1,
        nutrient_index=0.46,
        devices=("buoy", "gateway"),
    ),
    SiteSpec(
        code="LJ-GW-B01",
        name="官坞海带育苗区浮标",
        kind="nursery",
        longitude=119.8380,
        latitude=26.2820,
        region_code="LJ-DH",
        depth_m=9.0,
        summary="官坞村海带育苗核心区，连江海带育苗量约占全国 40%，需重点监控苗期水温与盐度。",
        temp_base=20.9,
        temp_amp=7.0,
        salinity_base=29.4,
        salinity_amp=1.6,
        do_base=7.6,
        turbidity_base=7.4,
        turbidity_amp=3.2,
        chl_base=2.8,
        nutrient_index=0.58,
        devices=("buoy", "camera"),
    ),
    SiteSpec(
        code="LJ-DH-B01",
        name="定海湾中部浮标",
        kind="farm",
        longitude=119.8580,
        latitude=26.2900,
        region_code="LJ-DH",
        depth_m=18.0,
        summary="定海湾中部，闽江口北岸，海域面积约 135 km²，陆源径流影响明显。",
        temp_base=20.8,
        temp_amp=6.8,
        salinity_base=28.6,
        salinity_amp=2.2,
        do_base=7.7,
        turbidity_base=9.2,
        turbidity_amp=4.0,
        chl_base=3.2,
        nutrient_index=0.64,
        devices=("buoy", "gateway"),
    ),
    SiteSpec(
        code="LJ-DH-P01",
        name="定海湾深远海平台监测点",
        kind="farm",
        longitude=119.8620,
        latitude=26.2880,
        region_code="LJ-DH",
        depth_m=26.0,
        summary="“百台万吨”深远海养殖工程试验区，定海湾 2 号平台养殖水体 1.58 万立方米，距岸约 2.5 km。",
        temp_base=20.7,
        temp_amp=6.6,
        salinity_base=29.8,
        salinity_amp=1.4,
        do_base=7.9,
        turbidity_base=6.1,
        turbidity_amp=2.4,
        chl_base=2.6,
        nutrient_index=0.50,
        devices=("buoy", "camera"),
    ),
    SiteSpec(
        code="LJ-TL-B01",
        name="苔菉北茭外海浮标",
        kind="open_sea",
        longitude=120.0250,
        latitude=26.3620,
        region_code="LJ-TL",
        depth_m=32.0,
        summary="北茭半岛外海，水交换条件好，赤潮外源输入监测前哨。",
        temp_base=20.2,
        temp_amp=6.4,
        salinity_base=31.6,
        salinity_amp=0.9,
        do_base=8.0,
        turbidity_base=4.2,
        turbidity_amp=2.0,
        chl_base=1.7,
        nutrient_index=0.36,
        devices=("buoy", "gateway"),
    ),
    SiteSpec(
        code="LJ-DLD-B01",
        name="东洛岛养殖平台浮标",
        kind="open_sea",
        longitude=120.0950,
        latitude=26.3620,
        region_code="LJ-DLD",
        depth_m=38.0,
        summary="东洛岛深远海养殖平台区，规划养殖水体约 17 万立方米，面向外海高海况环境。",
        temp_base=20.3,
        temp_amp=6.5,
        salinity_base=31.8,
        salinity_amp=0.8,
        do_base=8.1,
        turbidity_base=3.9,
        turbidity_amp=1.8,
        chl_base=1.6,
        nutrient_index=0.34,
        devices=("buoy", "camera", "gateway"),
    ),
    SiteSpec(
        code="LJ-MB-B01",
        name="马鼻浮曦红树林水质站",
        kind="mangrove",
        longitude=119.7220,
        latitude=26.4180,
        region_code="LJ-MB",
        depth_m=2.2,
        summary="罗源湾腹部潮滩，红树林分布区外缘，盐度受潮汐与径流交替控制。",
        temp_base=21.4,
        temp_amp=7.8,
        salinity_base=26.4,
        salinity_amp=3.6,
        do_base=7.2,
        turbidity_base=14.6,
        turbidity_amp=6.5,
        chl_base=3.4,
        nutrient_index=0.60,
        devices=("buoy", "camera"),
    ),
    SiteSpec(
        code="LJ-PK-B01",
        name="松坞敖江口红树林水质站",
        kind="estuary",
        longitude=119.6120,
        latitude=26.2760,
        region_code="LJ-PK",
        depth_m=1.8,
        summary="敖江入海口潮间带，径流淡水输入显著，是红树林生态与陆源污染协同监测点位。",
        temp_base=21.8,
        temp_amp=7.6,
        salinity_base=24.8,
        salinity_amp=4.4,
        do_base=7.0,
        turbidity_base=17.2,
        turbidity_amp=7.4,
        chl_base=3.8,
        nutrient_index=0.68,
        devices=("buoy", "camera"),
    ),
    SiteSpec(
        code="LJ-LYW-B01",
        name="鉴江松山红树林核心区水质站",
        kind="mangrove",
        longitude=119.7480,
        latitude=26.4650,
        region_code="LJ-LYW",
        depth_m=1.6,
        summary="连片天然红树林集中区（约 320 公顷）内部水道，用于长期连续的水质与湿地健康监测。",
        temp_base=21.2,
        temp_amp=7.7,
        salinity_base=27.2,
        salinity_amp=3.2,
        do_base=7.1,
        turbidity_base=13.4,
        turbidity_amp=5.8,
        chl_base=3.1,
        nutrient_index=0.55,
        devices=("buoy", "camera", "gateway"),
    ),
)

SITE_BY_CODE = {s.code: s for s in SITES}
REGION_BY_CODE = {r.code: r for r in REGIONS}


def devices_for_site(site: SiteSpec) -> list[dict]:
    """展开站点下的具体设备清单。"""

    suffix = {"buoy": "B", "camera": "C", "gateway": "G", "weather": "W", "uav": "U"}
    type_label = {
        "buoy": "多参数水质浮标",
        "camera": "高清岸基/平台摄像头",
        "gateway": "边缘计算网关",
        "weather": "小型气象站",
        "uav": "巡检无人机",
    }
    result: list[dict] = []
    for kind in site.devices:
        # 设备编码 = 站点编码 + 设备类型后缀，保证同一海域多个站点不会重号
        code = f"{site.code}-{suffix[kind]}"
        result.append(
            {
                "device_code": code,
                "name": f"{site.name.replace('浮标', '').replace('水质站', '')}{type_label[kind]}",
                "type": kind,
                "region_code": site.region_code,
                "longitude": site.longitude + (0.0016 if kind != "buoy" else 0.0),
                "latitude": site.latitude - (0.0012 if kind == "camera" else 0.0),
                "depth_m": 0.5 if kind in ("buoy", "gateway") else None,
                "sample_interval": 300 if kind == "buoy" else 900,
            }
        )
    return result


# ---------------------------------------------------------------------------
# 红树林遥感统计基线（依据公开资料的面积口径）
# ---------------------------------------------------------------------------
MANGROVE_BASELINE: tuple[dict, ...] = (
    {
        "region_code": "LJ-LYW",
        "period": "2026Q1",
        "area_ha": 320.0,
        "change_pct": 0.86,
        "health_score": 84.6,
        "health_level": "优",
        "ndvi_mean": 0.612,
        "ndwi_mean": 0.184,
        "data_source": "Sentinel-2 L2A",
        "remark": "罗源湾鉴江—松山连片天然红树林，福州唯一保存最完好的天然红树林湿地。",
    },
    {
        "region_code": "LJ-MB",
        "period": "2026Q1",
        "area_ha": 96.4,
        "change_pct": 1.42,
        "health_score": 81.2,
        "health_level": "良",
        "ndvi_mean": 0.578,
        "ndwi_mean": 0.196,
        "data_source": "Sentinel-2 L2A",
        "remark": "马鼻镇浮曦村红树林核心观赏区，处于罗源湾腹部滩涂。",
    },
    {
        "region_code": "LJ-PK",
        "period": "2026Q1",
        "area_ha": 43.8,
        "change_pct": -0.64,
        "health_score": 76.4,
        "health_level": "良",
        "ndvi_mean": 0.521,
        "ndwi_mean": 0.212,
        "data_source": "Sentinel-2 L2A",
        "remark": "敖江入海口红树林，受径流与陆源输入影响，需关注幼苗成活率。",
    },
)
