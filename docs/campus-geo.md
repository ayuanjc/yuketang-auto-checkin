# 某大学 · 校区坐标参考（位置签到用）

来源：OpenStreetMap Nominatim + Overpass（2026-11-16 抓取），campus way 170564706。
校区中心 22.0532745, 113.3986969，bbox 22.0426–22.0643 / 113.3903–113.4078。

坐标参考系 **WGS84**（OSM 原生）。雨课堂/腾讯地图用 **GCJ-02**，在华南沿海偏差约 400~600 米 —— 提交前必须做 WGS84→GCJ-02 转换，否则必然超出签到半径。

## 教学楼（OSM 有名字的建筑）

| 建筑 | 纬度 | 经度 | OSM building |
|---|---|---|---|
| 第二教学楼 | 22.0545632 | 113.39962 | university |
| 第三教学楼 | 22.0520086 | 113.4006328 | university |
| 实验楼（北） | 22.056992 | 113.4012291 | university |
| 实验楼（东北） | 22.0578399 | 113.4039645 | university |
| 艺术楼 | 22.044359 | 113.3957353 | university |
| 五环体育馆 | 22.0462831 | 113.3970942 | university |
| 第一饭堂 | 22.0485828 | 113.3942342 | university |
| 新饭 | 22.0603524 | 113.4023968 | university |
| 商学院教师办公楼 | 22.0531176 | 113.3944179 | university |
| 商学院（副楼） | 22.0541166 | 113.3957669 | university |

## 缺口

用户提到常用 **一教 / 二教 / 六教**，OSM 只有「第二教学楼」「第三教学楼」有名字；**第一教学楼、第六教学楼缺坐标**。

补齐方式（任选）：
1. 腾讯 LBS 地理编码 /api/v3/tencent-lbs/proxy（走教师端 key）
2. 与用户确认一教/六教相对二教的位置后按偏移估算
3. 位置签到首次成功后，用返回的 centerLat/centerLng 反查真实中心点（最准）

## 关键结论（协议层）

- GET /api/v3/classroom/lesson-config?universityId=&classroomId= 学生账号可访问，返回 {qrCodeConfig:{permission,userState}, locationCheckInConfig:{permission,userState,userPrefs}, aiCategoryConfig:{results}}；userPrefs 里是教师上次配置的 {centerLat, centerLng, radiusM, durationSec, timeoutMode, locationName}。实测学生账号 permission=1 / userState=0 / userPrefs={}（未开启时为空）。
- 教师开启位置签到：POST /api/v3/lesson/location-checkin/start，body 同 userPrefs 结构 + lessonId 上下文。
- 学生端提交位置签到：全量 1659 个 chunk 里签到相关路径只有 /api/v3/lesson/checkin、/api/v3/lesson/notkn/checkin、/api/v3/location-checkin/mobile-setup/{verify,detail,submit}、/api/v3/location-checkin/update-session、/api/v3/lesson/location-checkin/start。
  → 学生 GPS 上报极可能走 mobile-setup/submit（扫码 + 手机 GPS 的 H5 流程），而不是 lesson/checkin。
  → 若是这条，则服务端可用任意坐标重放，位置签到完全可在香港服务器完成，前提是拿到教师开的那个二维码/session。
