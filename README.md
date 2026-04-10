# DoNotPlay
DoNotPlay 是一款基于人工智能视觉的桌面工具，它通过监控疲劳与分心状态，帮你用高效的工作换取更多娱乐时间。An AI-powered desktop tool that prevents distractions by monitoring your posture and fatigue, helping you trade efficient work for more playtime.


🛡️ DoNotPlay - 你的专属 AI 专注力卫士
“为了毫无负罪感地尽情玩耍，请在此刻保持绝对专注。”

你是否也经常深受“注意力涣散”的困扰？明明打算工作或学习，却总是不由自主地拿起手机？DoNotPlay 就是为你量身打造的桌面级 AI 技术工具。

💡 核心理念：为什么叫 DoNotPlay？
时间对每个人都是绝对公平和恒定的。如果你在工作时间不停地摸鱼，实际上是在透支你本该用来休息和娱乐的时间。
虽然这个项目名叫 DoNotPlay（不要玩），但它的终极本意其实是 Play More（玩得更多）—— 帮助你在该专注的时候极度专注，从而为你省下更多的时间去尽情地、毫无负担地享受生活。

✨ 核心功能
本程序通过纯本地的计算机视觉技术（不上传任何隐私数据），将你的摄像头变成一位极其严格的“监考老师”：

👁️ 多维度视觉追踪：实时捕捉你的眼部视线（Gaze）、手部动作（Hands）以及头部姿态（Head Pose）。

📱 手机与分心监测：精准判断你是否在工作期间玩手机或左顾右盼。

💤 疲劳程度评估：通过监测眨眼频率（PERCLOS）和打哈欠、点头等动作，科学判断你的疲劳状态，并在你需要休息时发出提醒。

🎯 智能情景过滤：算法经过专门调优，尽可能降低了正常工作行为（如低头看书、敲击键盘、使用鼠标）带来的误判干扰。

🚀 当前状态 (Beta)
本项目目前仍处于早期开发阶段（Work in Progress），并不是一个完美的正式成品。它可能存在一些误报或不稳定的地方。
我非常欢迎大家下载尝试，把它当作你的“数字自律外脑”。如果你有任何改进建议或发现了 Bug，请随时提交 Issue 或 PR！

🛡️ DoNotPlay - Your AI-Powered Focus Guardian
"Focus fiercely now, so you can play guilt-free later."

Do you constantly struggle with shifting attention and digital distractions? DoNotPlay is a desktop AI tool engineered specifically for those who find it hard to stay in the zone.

💡 The Philosophy: Why "DoNotPlay"?
Time is a constant. When you procrastinate or get distracted during work hours, you are simply borrowing time from your future relaxation.
Despite the strict name DoNotPlay, the ultimate goal of this project is to help you Play More. By maximizing your efficiency and keeping you intensely focused when it matters, you earn back your time to rest and play without any guilt.

✨ Core Features
Using pure, local computer vision technology (your privacy is 100% safe—no data is uploaded), DoNotPlay turns your webcam into an uncompromising accountability partner:

👁️ Multi-Dimensional Tracking: Real-time analysis of your eye gaze, hand movements, and head posture.

📱 Distraction & Phone Detection: Instantly recognizes if you are using your phone or looking away from your workspace.

💤 Fatigue Assessment: Scientifically evaluates your fatigue levels by monitoring blink rates (PERCLOS), yawning, and head-nodding, reminding you when it's time to take a real break.

🎯 Smart Context Filtering: The algorithm is fine-tuned to minimize false positives during legitimate work activities, such as reading a physical book or typing on a keyboard.

🚀 Current Status (Beta)
Please note that this project is currently a Work in Progress (WIP) and not yet a finalized commercial product. You might encounter occasional bugs or false alerts.
Feel free to download it, try it out, and let it act as your digital "second brain" for self-discipline. Feedback, bug reports, and pull requests are highly welcome!


## 🙏 致谢 (Acknowledgements)

本项目的开发依赖于开源社区的贡献。特别感谢以下项目与工具：

* **[Focus Monitor](https://github.com/infinity811/focus-monitor)**：本项目的前端 UI 界面、番茄钟逻辑和数据保存功能，均参考并修改自该项目。感谢原作者（基于 MIT 许可证）提供的优秀代码！
* **[Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics)**：它为本项目提供了毫秒级的本地目标检测（如识别手机）能力（基于 AGPL-3.0 许可证）。
* **[Google MediaPipe](https://github.com/google/mediapipe)**：它提供了面部关键点与身体骨骼追踪技术。这是实现疲劳和体态检测的基础。

## 📄 开源许可证 (License)

本项目整体采用 **AGPL-3.0 License** 进行开源。
*(注：本项目前端部分修改自遵循 MIT 协议的 Focus Monitor)*
