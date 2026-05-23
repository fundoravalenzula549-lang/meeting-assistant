from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque


_GUI_IMPORT_ERROR: Exception | None = None
try:
    from PyQt6.QtCore import QPoint, Qt, QThread, pyqtSignal
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
except Exception as exc:  # pragma: no cover - optional desktop dependency
    _GUI_IMPORT_ERROR = exc


if _GUI_IMPORT_ERROR is None:

    class CaptionWorker(QThread):
        segment = pyqtSignal(dict)
        state = pyqtSignal(str)

        def __init__(self, url: str):
            super().__init__()
            self.url = url
            self._running = True

        def stop(self) -> None:
            self._running = False

        def run(self) -> None:
            try:
                import websocket
            except Exception as exc:
                self.state.emit(f"缺少 websocket-client: {exc}")
                return

            while self._running:
                ws = None
                try:
                    self.state.emit("连接中")
                    ws = websocket.create_connection(self.url, timeout=5)
                    self.state.emit("已连接")
                    while self._running:
                        raw = ws.recv()
                        if not raw:
                            continue
                        event = json.loads(raw)
                        if event.get("type") == "segment":
                            self.segment.emit(event.get("segment") or {})
                        elif event.get("type") == "completed":
                            self.state.emit("会议已完成")
                        elif event.get("type") == "error":
                            self.state.emit(event.get("message") or "服务端错误")
                except Exception as exc:
                    if self._running:
                        self.state.emit(f"等待重连: {exc}")
                        time.sleep(2)
                finally:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:
                            pass


    class CaptionWindow(QWidget):
        def __init__(self, url: str, max_lines: int):
            super().__init__()
            self._drag_pos: QPoint | None = None
            self._lines: deque[str] = deque(maxlen=max_lines)
            self._worker = CaptionWorker(url)

            self.setWindowTitle("Meeting Workbench Overlay")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.resize(960, 180)

            self.state = QLabel("未连接")
            self.state.setObjectName("state")
            self.caption = QLabel("等待字幕")
            self.caption.setObjectName("caption")
            self.caption.setWordWrap(True)
            self.translation = QLabel("")
            self.translation.setObjectName("translation")
            self.translation.setWordWrap(True)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 18, 24, 20)
            layout.setSpacing(8)
            layout.addWidget(self.state)
            layout.addWidget(self.caption)
            layout.addWidget(self.translation)

            font = QFont()
            font.setPointSize(26)
            font.setWeight(QFont.Weight.DemiBold)
            self.caption.setFont(font)

            self.setStyleSheet(
                """
                QWidget {
                    background: rgba(12, 16, 24, 190);
                    border-radius: 14px;
                }
                QLabel {
                    color: #f8fafc;
                    background: transparent;
                }
                QLabel#state {
                    color: #93c5fd;
                    font-size: 13px;
                }
                QLabel#caption {
                    line-height: 1.28;
                }
                QLabel#translation {
                    color: #cbd5e1;
                    font-size: 18px;
                }
                """
            )
            self._worker.segment.connect(self.on_segment)
            self._worker.state.connect(self.state.setText)
            self._worker.start()

        def on_segment(self, segment: dict) -> None:
            source = "我方" if segment.get("source") == "mic" else "对方"
            speaker = segment.get("speaker") or source
            text = " ".join(str(segment.get("text") or "").split())
            if not text:
                return
            self._lines.append(f"{source} · {speaker}: {text}")
            self.caption.setText("\n".join(self._lines))
            translation = " ".join(str(segment.get("translation") or "").split())
            self.translation.setText(translation)

        def mousePressEvent(self, event) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

        def mouseMoveEvent(self, event) -> None:
            if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                event.accept()

        def mouseReleaseEvent(self, event) -> None:
            self._drag_pos = None
            event.accept()

        def closeEvent(self, event) -> None:
            self._worker.stop()
            self._worker.wait(1500)
            event.accept()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="meeting-workbench overlay")
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--max-lines", type=int, default=2)
    args = parser.parse_args(argv)

    if _GUI_IMPORT_ERROR is not None:
        print(f"桌面悬浮字幕需要安装可选依赖: {_GUI_IMPORT_ERROR}", file=sys.stderr)
        print("运行: pip install '.[overlay]'", file=sys.stderr)
        raise SystemExit(2)

    app = QApplication(sys.argv[:1])
    window = CaptionWindow(args.url, max(1, args.max_lines))
    screen = app.primaryScreen().availableGeometry()
    window.move((screen.width() - window.width()) // 2, screen.height() - window.height() - 80)
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
