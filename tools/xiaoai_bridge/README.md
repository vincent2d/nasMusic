# 小爱音箱播放兼容补丁

`playback_compat.py` 用于当前 NAS 的 MiAirX 桥接容器，处理 fnAudio
`http://<桥接 hostname>:53199/upnp/media/…` 来源。其他来源继续使用桥接原有轮询与结束处理。

修正内容：

- 展开旧版 MiNA 的 `data.info` JSON 响应，使用音箱真实位置、时长和音量。
- 音箱原生采用单次播放（模式 2），队列和循环由 fnAudio 管理。
- 以真实播完状态交接下一首，绕过桥接按墙钟计时结束和等待 NextURI 的路径。
- fnAudio 监控器兼容 L05B 在 EOF 上报暂停的行为；用户暂停和中途暂停不会触发续播。
- 早停、状态查询失败或位置停滞报告错误并冻结进度；不冒充自然结束。
- 丢弃跨换源、Play 和 Seek 的旧查询结果；Seek 尾段保持原曲逻辑偏移。

部署时将该文件只读挂载到容器内
`/usr/local/lib/python3.12/site-packages/miairx/fnaudio_playback_compat.py`。
现有兼容版 `miairx/protocols/dlna/server.py` 导入
`configure_renderer`，并在 `register_renderer` 设置代理回调后调用
`configure_renderer(renderer, self.hostname)`。

当前核验镜像固定为
`jxydk/miairx@sha256:34b30ae4dde05dd50160969be3196e36030621038e8e16bc1ed313660698bd01`。
桥接配置、补丁前后源码、回滚和真实设备验证记录见
`artifacts/xiaoai-playback-20261119/`。此补丁不进入 fnAudio FPK；NAS 的桥接
Compose 通过持久只读挂载保留补丁，升级桥接镜像时需重新验证这些接口。

回归测试：从 `src/` 运行
`PYTHONPATH=app/server:. python3 -m unittest tests.test_xiaoai_bridge_compat`。
