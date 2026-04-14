<div align="center">

[![QQ Group](https://img.shields.io/badge/QQ-672723869-12B7F5?style=for-the-badge&logo=tencentqq&logoColor=white)](https://qm.qq.com/q/KsXFPJskGQ)
[![Download](https://img.shields.io/badge/Download-DoNotPlay_v1.0._Windows.zip-green?style=for-the-badge&logo=github)](https://github.com/DOOZH/DoNotPlay/releases/download/%E6%AD%A3%E5%BC%8F%E7%89%88/DoNotPlay_v1.0._Windows.zip)
[![Telegram](https://img.shields.io/badge/Telegram-Join_Channel-blue?style=for-the-badge&logo=telegram)](https://t.me/+x_h5I3Ns0a8xOWI1)

</div>



这是一款帮你保持专注的桌面小工具。我的初衷是：在工作或者学习时间娱乐事实上既耽误了正事也玩不开心，所以尝试开发了DoNotPlay来帮助我在应该专注的时间实现专注。这样可以省出更多的时间玩！

所有数据处理都在你自己的电脑上完成，不会上传任何东西，放心用。


▸ 怎么安装？

不用安装。下载DoNotPlay_v1.0._Windows.zip并解压，把整个文件夹（DoNotPlay.exe + _internal）放在你喜欢的地方，
双击 DoNotPlay.exe 就能用了。

注意：DoNotPlay.exe 和 _internal 文件夹必须放在一起，缺一不可。


▸ 第一次打开

1. 程序会自动打开一个窗口，同时启动摄像头
2. 坐好、看着屏幕，点击右上角的“校准”，等几秒钟让它完成校准
3. 校准完成后就开始监测了，你该干啥干啥

如果看到"需要摄像头"的提示，检查一下摄像头是不是被其他软件占用了。


▸ 它能帮你做什么？

  专注追踪 — 记录你每天专注了多久，帮你了解自己的状态
  走神提醒 — 发现你走神超过 5 秒，弹个窗提醒你
  手机拦截 — 检测到你在玩手机，立刻弹出警告
  驼背纠正 — 持续驼背超过 10 秒会提醒你坐直
  疲劳预警 — 检测到你很困了，建议你休息一下
  喝水提醒 — 超过 1 小时没喝水会提醒你
  离开检测 — 你离开后自动暂停，回来后自动恢复


▸ 右上角按钮说明

  灵敏度 — 调整检测灵敏度（严格/标准/宽松/极简）
           如果你有多块屏幕，建议选"宽松"，转头看副屏不会误判
  声音   — 开关提示音
  主题   — 切换深色/浅色模式


▸ 休息功能

右侧面板有个休息区。设好时间，点"开始"就能进入休息模式。
休息期间所有监测和提醒都会暂停。时间到了自动恢复。

如果收到疲劳提醒，弹窗上也有"进入休息"按钮，一键休息。
不想再被疲劳提醒烦？点"本次关闭疲惫监测"就行。


▸ 常见问题

  Q: 程序打不开 / 闪退？
  A: 确保 _internal 文件夹和 exe 在同一目录下。
     如果还是不行，试试右键"以管理员身份运行"。

  Q: 摄像头画面是歪的？
  A: 如果你的摄像头是竖着装的，程序会自动旋转。
     目前默认旋转 270°，如果画面方向不对，
     可以编辑 config.json 里的 "camera_rotation" 改成 0/90/180/270。

  Q: 老是误判我走神？
  A: 把灵敏度调低一档试试。有多块屏幕的话建议用"宽松"模式。

  Q: 数据存在哪？
  A: 就在当前文件夹的 data.json 里。删掉它就是重新开始。


▸ 小贴士

  - 开始使用前坐好别动，让它校准几秒钟，后续检测会更准
  - 光线充足的环境下效果最好

    有问题可以去任何你能找到我的地方反馈




祝你专注愉快!



  技术路径说明（给好奇的你）


DoNotPlay 是一个纯本地运行的 AI 专注力监测工具，不联网，不上传数据。

▸ 技术栈

  后端：Python + WebSocket（实时数据推送）
  前端：原生 JavaScript / HTML / CSS（通过 pywebview 内嵌浏览器渲染）
  视觉模型：
    - YOLO26n 物体检测 — 识别手机、水杯、键盘等日常物品
    - YOLO26n-pose 姿态检测 — 追踪肩膀、手腕等 17 个人体关键点
    - MediaPipe Face Mesh — 468 个面部特征点，用于眼部状态和头部朝向
  推理引擎：ONNX Runtime（优先 DirectML/CUDA GPU 加速，无显卡时自动回退 CPU）

▸ 工作原理

  1. 摄像头以约 30fps 采集画面
  2. 三个模型协同推理，提取人脸朝向、眼睛开合、身体姿态、手持物体
  3. 后端通过多路信号融合 + 状态机 + 消抖算法，
     判定当前状态（专注/走神/手机/离开/疲劳/驼背/喝水）
  4. 结果通过 WebSocket 实时推送到前端，前端更新 UI 并触发提醒

▸ 核心检测逻辑

  专注：面部朝向屏幕，头部偏转在灵敏度阈值内
  走神：头部偏转超阈值 + 持续一定时间后触发
  手机：YOLO 检测到手机后，融合 6 路信号判定（距离/视线/低头/手臂/遮挡推断/持有）
  喝水：YOLO 检测到杯子 + 手臂举起 + 杯子移动 / 头部后仰
  疲劳：PERCLOS（眼睛闭合时间占比）+ 长眨眼频率 + 哈欠检测，EMA 平滑评分
  驼背：躯干前倾角度超过 17°，持续 10 秒以上
  肩颈：校准时记录基线角度，运行时自动补偿摄像头安装偏差
  离开：面部消失超过设定时间，自动暂停计时

▸ 隐私

  所有计算在本地完成，不需要网络。
  不保存摄像头画面，不上传数据。
  持久化文件仅有 data.json（统计数据）和 config.json（偏好设置）。



本项目的开发依赖于开源社区的贡献。特别感谢以下项目与工具：

* **[Focus Monitor](https://github.com/infinity811/focus-monitor)**
* **[Ultralytics YOLO26](https://github.com/ultralytics/ultralytics)**
* **[Google MediaPipe](https://github.com/google/mediapipe)**


开源许可证 (License)
本项目整体采用 **AGPL-3.0 License** 进行开源。
