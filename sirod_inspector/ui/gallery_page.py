"""
缺陷图库页面
============
- FlowLayout 横向排满自动换行
- 点击缩略图弹出大图，支持滚轮缩放和拖拽平移
- 双击恢复原始大小
"""
import logging
import os

from PyQt6.QtCore import Qt, QRect, QSize, QPoint, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QPixmap, QWheelEvent, QMouseEvent, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QComboBox, QScrollArea, QLayout,
    QFrame, QDialog, QSizePolicy, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  FlowLayout — 横向排满自动换行
# ─────────────────────────────────────────────
class FlowLayout(QLayout):
    """自动换行的流式布局，子项横向排列，排满后自动换行。"""

    def __init__(self, parent=None, margin=0, spacing=10):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def insertWidget(self, index: int, widget: QWidget):
        """在指定位置插入一个 widget（用于新增时插入到最前面）"""
        from PyQt6.QtWidgets import QWidgetItem
        item = QWidgetItem(widget)
        widget.setParent(self.parentWidget())
        self._items.insert(index, item)
        self.invalidate()
        self.update()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only=False):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            item_size = item.sizeHint()
            next_x = x + item_size.width() + self._spacing
            if next_x - self._spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + item_size.width() + self._spacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            line_height = max(line_height, item_size.height())

        return y + line_height - rect.y() + m.bottom()


# ─────────────────────────────────────────────
#  ZoomableImageDialog — 弹出大图查看器（支持前后切换）
# ─────────────────────────────────────────────
class ZoomableImageDialog(QDialog):
    """
    弹出式大图查看器。
    - 默认以最大尺寸（全屏）展示
    - 支持滚轮缩放、拖拽平移、双击适应窗口
    - 支持前后切换（← → 键或底部按钮）
    """

    def __init__(self, items: list[dict], current_index: int = 0, parent=None):
        """
        items: list of {"rod_id", "defect_type", "image_path"}
        current_index: 当前显示的图片索引
        """
        super().__init__(parent)
        self._items        = items
        self._current_idx  = current_index
        self._scale_factor = 1.0

        self.setWindowTitle("缺陷大图")
        self.setStyleSheet("QDialog { background-color: #0d1b2a; }")
        self.setMinimumSize(640, 480)
        # 默认屏幕一半，可自由拖拽调整大小
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen().availableGeometry()
        w = screen.width() // 2
        h = screen.height() // 2
        self.resize(w, h)
        self.move(screen.x() + screen.width() // 4, screen.y() + screen.height() // 4)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # 标题栏
        self._title_label = QLabel()
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setStyleSheet(
            "color: #e0e0e0; font-size: 13px; font-weight: bold; padding: 4px;"
        )
        layout.addWidget(self._title_label)

        # 图像区
        self._scene       = QGraphicsScene(self)
        self._view        = QGraphicsView(self._scene, self)
        self._view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self._view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._view.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self._view.setStyleSheet(
            "QGraphicsView { border: none; background-color: #0a1520; }"
        )
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)
        layout.addWidget(self._view, stretch=1)

        # 底部控制栏
        ctrl = QHBoxLayout()
        ctrl.setSpacing(12)

        self._btn_prev = QPushButton("◀  上一张")
        self._btn_prev.setFixedSize(110, 34)
        self._btn_prev.setStyleSheet(self._btn_style())
        self._btn_prev.clicked.connect(self._go_prev)
        ctrl.addWidget(self._btn_prev)

        self._index_label = QLabel()
        self._index_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._index_label.setStyleSheet("color: #8888a0; font-size: 12px;")
        self._index_label.setFixedWidth(120)
        ctrl.addWidget(self._index_label)

        self._btn_next = QPushButton("下一张  ▶")
        self._btn_next.setFixedSize(110, 34)
        self._btn_next.setStyleSheet(self._btn_style())
        self._btn_next.clicked.connect(self._go_next)
        ctrl.addWidget(self._btn_next)

        ctrl.addStretch()

        hint = QLabel("滚轮缩放  |  拖拽平移  |  双击适应窗口  |  ← → 切换  |  Esc 关闭")
        hint.setStyleSheet("color: #555566; font-size: 11px;")
        ctrl.addWidget(hint)

        layout.addLayout(ctrl)

        # 安装事件过滤器（滚轮缩放）
        self._view.viewport().installEventFilter(self)

        # 加载当前图片
        self._load_current()

    @staticmethod
    def _btn_style() -> str:
        return (
            "QPushButton { background-color: #1e2d45; color: #c0c8d8; border: 1px solid #0f3460;"
            " border-radius: 5px; font-size: 12px; }"
            "QPushButton:hover { background-color: #0f3460; color: white; }"
            "QPushButton:disabled { background-color: #111820; color: #444455; border-color: #222233; }"
        )

    def _load_current(self):
        """加载并显示当前索引的图片"""
        if not self._items:
            return

        item      = self._items[self._current_idx]
        rod_id    = item.get("rod_id", "")
        defect    = item.get("defect_type", "")
        img_path  = item.get("image_path", "")

        self._title_label.setText(
            f"缺陷大图  —  {rod_id}  [{defect}]"
            + (f"  |  {os.path.basename(img_path)}" if img_path else "  |  无图像")
        )
        self._index_label.setText(
            f"{self._current_idx + 1} / {len(self._items)}"
        )
        self._btn_prev.setEnabled(self._current_idx > 0)
        self._btn_next.setEnabled(self._current_idx < len(self._items) - 1)

        # 加载图片
        self._scene.clear()
        self._pixmap_item = QGraphicsPixmapItem()
        self._scene.addItem(self._pixmap_item)

        if img_path and os.path.isfile(img_path):
            pix = QPixmap(img_path)
            if not pix.isNull():
                self._pixmap_item.setPixmap(pix)
                self._scene.setSceneRect(self._pixmap_item.boundingRect())
                self._view.resetTransform()
                self._scale_factor = 1.0
                self._view.fitInView(
                    self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio
                )
                return

        # 无图像时显示占位文字
        self._scene.setSceneRect(0, 0, 800, 500)
        text = self._scene.addText("无可用图像")
        text.setDefaultTextColor(QColor("#555566"))

    def _go_prev(self):
        if self._current_idx > 0:
            self._current_idx -= 1
            self._load_current()

    def _go_next(self):
        if self._current_idx < len(self._items) - 1:
            self._current_idx += 1
            self._load_current()

    def eventFilter(self, obj, event):
        if obj == self._view.viewport() and isinstance(event, QWheelEvent):
            delta  = event.angleDelta().y()
            factor = 1.15 if delta > 0 else 1 / 1.15
            self._scale_factor *= factor
            if 0.05 < self._scale_factor < 50:
                self._view.scale(factor, factor)
            else:
                self._scale_factor /= factor
            return True
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event):
        """双击恢复适应窗口"""
        self._view.resetTransform()
        self._scale_factor = 1.0
        self._view.fitInView(
            self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio
        )

    def resizeEvent(self, event):
        """窗口大小改变时重新适应图像"""
        super().resizeEvent(event)
        if self._pixmap_item and not self._pixmap_item.pixmap().isNull():
            self._view.fitInView(
                self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio
            )
            self._scale_factor = 1.0

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        elif event.key() == Qt.Key.Key_Left:
            self._go_prev()
        elif event.key() == Qt.Key.Key_Right:
            self._go_next()
        else:
            super().keyPressEvent(event)


# ─────────────────────────────────────────────
#  DefectCard — 缺陷图片卡片
# ─────────────────────────────────────────────
class DefectCard(QFrame):
    """缺陷图片卡片，点击可弹出大图"""

    clicked = pyqtSignal()

    def __init__(self, rod_id, defect_type, timestamp, image_path=None,
                 image_list: list = None, list_index: int = 0):
        super().__init__()
        self._image_path  = image_path
        self._rod_id      = rod_id
        self._defect_type = defect_type
        self._image_list  = image_list or []   # 完整列表，供切换用
        self._list_index  = list_index          # 本卡片在列表中的位置

        self.setStyleSheet(
            "QFrame { background-color: #16213e; border: 1px solid #0f3460; "
            "border-radius: 8px; }"
            "QFrame:hover { border: 1px solid #00d4ff; }"
        )
        self.setFixedSize(220, 240)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        # 缩略图
        self._img_label = QLabel()
        self._img_label.setFixedSize(208, 170)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet(
            "background-color: #0d1b2a; border: 1px solid #0f3460; border-radius: 4px;"
        )
        if image_path and os.path.isfile(image_path):
            pix = QPixmap(image_path).scaled(
                208, 170,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_label.setPixmap(pix)
        else:
            self._img_label.setText("无图像")
            self._img_label.setStyleSheet(
                self._img_label.styleSheet() + " color: #555566; font-size: 12px;"
            )
        layout.addWidget(self._img_label)

        # 信息行
        info_layout = QHBoxLayout()
        info_layout.setContentsMargins(4, 0, 4, 0)
        info_layout.setSpacing(4)

        rod_lbl = QLabel(rod_id)
        rod_lbl.setStyleSheet(
            "font-size: 11px; color: #00d4ff; font-weight: bold;"
            " background: transparent; border: none;"
        )
        info_layout.addWidget(rod_lbl)

        # 缺陷类型标签 — 不同类型不同颜色
        type_color = self._get_type_color(defect_type)
        type_lbl = QLabel(defect_type or "未知")
        type_lbl.setStyleSheet(
            f"font-size: 10px; background-color: {type_color}; color: white; "
            "border-radius: 3px; padding: 1px 6px;"
        )
        info_layout.addWidget(type_lbl)
        info_layout.addStretch()

        time_lbl = QLabel(timestamp)
        time_lbl.setStyleSheet(
            "font-size: 10px; color: #8888a0; background: transparent; border: none;"
        )
        info_layout.addWidget(time_lbl)

        layout.addLayout(info_layout)

    @staticmethod
    def _get_type_color(defect_type: str) -> str:
        """根据缺陷类型返回对应颜色"""
        colors = {
            "隐裂": "#e74c3c",
            "崩边": "#e67e22",
        }
        return colors.get(defect_type, "#8e44ad")

    def mouseDoubleClickEvent(self, event):
        """双击打开大图查看器"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._show_large_image()
        super().mouseDoubleClickEvent(event)

    def _show_large_image(self):
        """弹出大图查看器（携带完整列表支持前后切换）"""
        if self._image_list:
            dialog = ZoomableImageDialog(
                self._image_list, self._list_index, self.window()
            )
            dialog.exec()
        elif self._image_path and os.path.isfile(self._image_path):
            # 兼容单张图片
            dialog = ZoomableImageDialog(
                [{"rod_id": self._rod_id, "defect_type": self._defect_type,
                  "image_path": self._image_path}],
                0, self.window()
            )
            dialog.exec()
        else:
            logger.debug("无可用图像，跳过大图显示")


# ─────────────────────────────────────────────
#  GalleryPage — 缺陷图库页面
# ─────────────────────────────────────────────
class GalleryPage(QWidget):
    """缺陷图库页面 — FlowLayout 横向排满换行"""

    # 跨线程安全信号：从 TCP 线程传递缺陷数据到 UI 线程
    _add_defect_signal    = pyqtSignal(str, str, str, object)  # rod_id, defect_type, timestamp, image_path
    _update_image_signal  = pyqtSignal(str, str)               # rod_id, image_path

    def __init__(self):
        super().__init__()
        self._defect_items = []
        self._defect_types = ["全部", "隐裂", "崩边"]  # 默认缺陷类型
        self._init_ui()

        # 连接信号到槽函数（确保 UI 操作在主线程执行）
        self._add_defect_signal.connect(self._on_add_defect)
        self._update_image_signal.connect(self._on_update_image)

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 顶部筛选栏
        filter_frame = QFrame()
        filter_frame.setStyleSheet(
            "QFrame { background-color: #16213e; border: 1px solid #0f3460; "
            "border-radius: 8px; padding: 8px; }"
        )
        fl = QHBoxLayout(filter_frame)
        fl.setSpacing(10)

        fl.addWidget(QLabel("缺陷类型:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(self._defect_types)
        self._type_combo.setFixedWidth(120)
        self._type_combo.currentTextChanged.connect(self._filter_cards)
        fl.addWidget(self._type_combo)

        fl.addStretch()

        self._count_label = QLabel("共 0 条缺陷记录")
        self._count_label.setStyleSheet("color: #8888a0;")
        fl.addWidget(self._count_label)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._filter_cards)
        fl.addWidget(refresh_btn)

        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(self._clear_all)
        fl.addWidget(clear_btn)

        layout.addWidget(filter_frame)

        # 滚动区域
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { border: none; }")

        # 创建初始的 flow widget
        self._flow_widget = self._create_flow_widget()
        self._scroll.setWidget(self._flow_widget)

        layout.addWidget(self._scroll, 1)

    def _create_flow_widget(self) -> QWidget:
        """创建一个新的带 FlowLayout 的容器 widget"""
        widget = QWidget()
        flow_layout = FlowLayout(widget, margin=4, spacing=10)
        widget.setLayout(flow_layout)
        return widget

    # ─────────── 公开接口 ───────────

    def add_defect(self, rod_id, defect_type, timestamp, image_path=None):
        """
        添加一条缺陷记录（线程安全）。

        可从任意线程调用，通过信号安全传递到 UI 线程执行。
        """
        self._add_defect_signal.emit(rod_id, defect_type, timestamp, image_path)

    def update_image(self, rod_id: str, image_path: str):
        """
        更新指定晶棒编号的卡片图像（线程安全）。

        后台线程保存图像后调用此方法，通知 UI 线程刷新对应卡片的图片显示。
        """
        if image_path:
            self._update_image_signal.emit(rod_id, image_path)

    @pyqtSlot(str, str)
    def _on_update_image(self, rod_id: str, image_path: str):
        """在 UI 线程中更新 _defect_items 中的图像路径，并刷新对应卡片"""
        # 更新数据列表中的路径
        for item in self._defect_items:
            if item["rod_id"] == rod_id and not item.get("image_path"):
                item["image_path"] = image_path
                break

        # 找到 flow_widget 中对应的卡片并刷新图片
        layout = self._flow_widget.layout()
        for i in range(layout.count()):
            widget_item = layout.itemAt(i)
            if widget_item is None:
                continue
            card = widget_item.widget()
            if isinstance(card, DefectCard) and card._rod_id == rod_id and not card._image_path:
                card._image_path = image_path
                if os.path.isfile(image_path):
                    pix = QPixmap(image_path).scaled(
                        208, 170,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    card._img_label.setPixmap(pix)
                    card._img_label.setText("")
                break

    @pyqtSlot(str, str, str, object)
    def _on_add_defect(self, rod_id, defect_type, timestamp, image_path):
        """在 UI 线程中实际添加缺陷记录（只插入一张新卡片，不重建全部）"""
        self._defect_items.insert(0, {
            "rod_id": rod_id,
            "defect_type": defect_type,
            "timestamp": timestamp,
            "image_path": image_path,
        })

        # 超出上限时裁剪（避免内存无限增长）
        if len(self._defect_items) > 500:
            self._defect_items = self._defect_items[:500]

        type_filter = self._type_combo.currentText()
        if type_filter != "全部" and defect_type != type_filter:
            # 当前筛选不包含此类型，只更新计数，不插入卡片
            filtered_count = sum(
                1 for d in self._defect_items if d["defect_type"] == type_filter
            )
            self._count_label.setText(f"共 {filtered_count} 条缺陷记录")
            return

        # 在 flow_widget 最前面插入一张新卡片（不重建整个列表）
        flow_layout = self._flow_widget.layout()
        # 构建当前筛选后的完整图片列表，供切换用
        filtered = self._defect_items if type_filter == "全部" else [
            d for d in self._defect_items if d["defect_type"] == type_filter
        ]
        img_list = [
            {"rod_id": d["rod_id"], "defect_type": d["defect_type"],
             "image_path": d.get("image_path", "")}
            for d in filtered
        ]
        card = DefectCard(
            rod_id, defect_type, timestamp, image_path,
            image_list=img_list, list_index=0   # 新卡片插在最前面，索引为 0
        )
        flow_layout.insertWidget(0, card)

        # 更新计数
        type_filter = self._type_combo.currentText()
        if type_filter == "全部":
            self._count_label.setText(f"共 {len(self._defect_items)} 条缺陷记录")
        else:
            filtered_count = sum(
                1 for d in self._defect_items if d["defect_type"] == type_filter
            )
            self._count_label.setText(f"共 {filtered_count} 条缺陷记录")

        self._flow_widget.adjustSize()

    def set_defect_types(self, types: list):
        """更新缺陷类型下拉列表（由设置页面调用）"""
        self._defect_types = ["全部"] + [t for t in types if t != "全部"]
        current = self._type_combo.currentText()
        self._type_combo.blockSignals(True)
        self._type_combo.clear()
        self._type_combo.addItems(self._defect_types)
        # 恢复之前选中的类型
        idx = self._type_combo.findText(current)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.blockSignals(False)

    # ─────────── 内部方法 ───────────

    def _filter_cards(self):
        """
        根据筛选条件重新渲染卡片。

        为避免 FlowLayout 的 C++ 对象被提前销毁导致 RuntimeError，
        采用"替换整个 flow_widget"的策略，而非逐个清除 layout 中的 item。
        旧的 widget 通过 deleteLater 安全回收。
        """
        # 创建全新的 flow widget 和 layout
        new_widget = self._create_flow_widget()
        new_layout = new_widget.layout()

        type_filter = self._type_combo.currentText()
        filtered = self._defect_items
        if type_filter != "全部":
            filtered = [d for d in filtered if d["defect_type"] == type_filter]

        self._count_label.setText(f"共 {len(filtered)} 条缺陷记录")

        # 构建完整图片列表（供大图切换用）
        img_list = [
            {"rod_id": d["rod_id"], "defect_type": d["defect_type"],
             "image_path": d.get("image_path", "")}
            for d in filtered[:500]
        ]

        # 限制显示数量，避免内存溢出
        for idx, item in enumerate(filtered[:500]):
            card = DefectCard(
                item["rod_id"], item["defect_type"],
                item["timestamp"], item.get("image_path"),
                image_list=img_list, list_index=idx,
            )
            new_layout.addWidget(card)

        # 替换旧的 flow widget
        old_widget = self._flow_widget
        self._flow_widget = new_widget
        self._scroll.setWidget(new_widget)

        # 旧 widget 安全销毁（setWidget 会自动 take ownership，
        # 但旧 widget 需要手动销毁）
        if old_widget is not None:
            old_widget.deleteLater()

        # 强制更新布局
        new_widget.adjustSize()

    def _clear_all(self):
        """清空所有缺陷记录"""
        self._defect_items.clear()
        self._filter_cards()