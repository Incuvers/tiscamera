# Copyright 2017 The Imaging Source Europe GmbH
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import functools
from tcam_capture.ImageSaver import ImageSaver
from tcam_capture.VideoSaver import VideoSaver
from tcam_capture.PropertyWidget import PropertyWidget, Prop
from tcam_capture.TcamSignal import TcamSignals
from tcam_capture.TcamCaptureData import TcamCaptureData
from PyQt5 import QtGui, QtWidgets, QtCore
from PyQt5.QtWidgets import (QApplication, QWidget, QDialog,
                             QHBoxLayout, QVBoxLayout,
                             QAction, QMenu, QGraphicsView,
                             QGraphicsItem, QGraphicsScene, QGraphicsPixmapItem)

from PyQt5.QtCore import QObject, pyqtSignal, Qt, QEvent, QMutex

import logging

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Tcam", "0.1")
gi.require_version("GstVideo", "1.0")

from gi.repository import Tcam, Gst, GLib, GstVideo

log = logging.getLogger(__name__)


class ViewItem(QtWidgets.QGraphicsPixmapItem):
    """Derived class enables mouse tracking for color under mouse retrieval"""
    def __init__(self, parent=None):
        super(ViewItem, self).__init__(parent)
        self.setAcceptHoverEvents(True)
        self.mouse_over = False  # flag if mouse is over our widget
        self.mouse_position_x = -1
        self.mouse_position_y = -1

    def hoverEnterEvent(self, event):
        self.mouse_over = True

    def hoverLeaveEvent(self, event):
        self.mouse_over = False

    def hoverMoveEvent(self, event):
        mouse_position = event.pos()

        self.mouse_position_x = mouse_position.x()
        self.mouse_position_y = mouse_position.y()
        super().hoverMoveEvent(event)

    def get_mouse_color(self):
        if self.mouse_over:
            if(self.mouse_position_x <= self.pixmap().width() and
               self.mouse_position_y <= self.pixmap().height()):
                return self.pixmap().toImage().pixelColor(self.mouse_position_x,
                                                          self.mouse_position_y)
            else:
                self.mouse_position_x = -1
                self.mouse_position_y = -1
        else:
            return QtGui.QColor(0, 0, 0)


class TcamScreen(QtWidgets.QGraphicsView):

    new_pixmap = pyqtSignal(QtGui.QPixmap)
    new_pixel_under_mouse = pyqtSignal(bool, QtGui.QColor)
    destroy_widget = pyqtSignal()

    def __init__(self, parent=None):
        super(TcamScreen, self).__init__(parent)
        self.setMouseTracking(True)
        self.mutex = QMutex()
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Expanding)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setFrameStyle(0)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.new_pixmap.connect(self.on_new_pixmap)
        self.pix = ViewItem()
        self.scene.addItem(self.pix)

        self.factor = 1.0
        self.pix_width = 0
        self.pix_height = 0

        self.orig_parent = None
        self.is_fullscreen = False
        self.scale_factor = 1.0
        self.scene_position_x = None
        self.scene_position_y = None

        self.mouse_position_x = -1
        self.mouse_position_y = -1

        self.zoom_factor = 1.0

    def on_new_pixmap(self, pixmap):
        self.pix.setPixmap(pixmap)
        self.send_mouse_pixel()

    def send_mouse_pixel(self):

        self.new_pixel_under_mouse.emit(self.pix.mouse_over,
                                        self.pix.get_mouse_color())

    def mouseMoveEvent(self, event):
        mouse_position = event.pos()
        self.mouse_position_x = mouse_position.x()
        self.mouse_position_y = mouse_position.y()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        # Zoom Factor
        zoomInFactor = 1.25
        zoomOutFactor = 1 / zoomInFactor

        # Set Anchors
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)

        # Save the scene pos
        oldPos = self.mapToScene(event.pos())

        # Zoom
        if event.angleDelta().y() > 0:
            zoomFactor = zoomInFactor
        else:
            zoomFactor = zoomOutFactor

        # restrict zooming out
        if self.zoom_factor * zoomFactor <= 0.64:
            return

        self.zoom_factor *= zoomFactor

        # we scale the view itself to get infinite zoom
        # so that we can inspect a single pixel
        self.scale(zoomFactor, zoomFactor)

        # Get the new position
        newPos = self.mapToScene(event.pos())

        # Move scene to old position
        delta = newPos - oldPos
        self.scene_position_x = delta.x()
        self.scene_position_y = delta.y()
        self.translate(delta.x(), delta.y())

    def set_scale_position(self, scale_factor, x, y):
        self.scale(scale_factor, scale_factor)
        self.translate(x, y)

    def keyPressEvent(self, event):
        if self.isFullScreen():
            if (event.key() == Qt.Key_F11
                or event.key() == Qt.Key_Escape
                or event.key() == Qt.Key_F):
                self.destroy_widget.emit()
        else:
            # ignore event so that parent widgets can use it
            event.ignore()


class TcamView(QWidget):

    image_saved = pyqtSignal(str)
    new_pixel_under_mouse = pyqtSignal(bool, QtGui.QColor)

    def __init__(self, serial, parent=None):
        super(TcamView, self).__init__(parent)
        self.layout = QHBoxLayout()
        self.container = TcamScreen(self)
        self.container.new_pixel_under_mouse.connect(self.new_pixel_under_mouse_slot)
        self.fullscreen_container = None  # separate widget for fullscreen usage
        self.is_fullscreen = False

        self.layout.addWidget(self.container)
        self.layout.setSizeConstraint(QtWidgets.QLayout.SetMaximumSize)
        self.setLayout(self.layout)
        self.serial = serial
        self.data = TcamCaptureData()
        self.pipeline = None
        self.image = None
        self.mouse_is_pressed = False
        self.current_width = 0
        self.current_height = 0
        self.device_lost_callbacks = []
        self.format_menu = None

    def new_pixel_under_mouse_slot(self, active: bool, color: QtGui.QColor):
        self.new_pixel_under_mouse.emit(active, color)

    def eventFilter(self, obj, event):
        """"""
        if event.type == QEvent.KeyPress:
            if event.key() == Qt.Key_F11:
                self.toggle_fullscreen()
                return True

        return QObject.eventFilter(self, obj, event)

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.is_fullscreen = False
            self.showNormal()
            self.fullscreen_container.hide()
            self.fullscreen_container.deleteLater()
            self.fullscreen_container = None
        else:
            self.is_fullscreen = True
            self.fullscreen_container = TcamScreen()
            self.fullscreen_container.showFullScreen()
            self.fullscreen_container.show()
            self.fullscreen_container.setFocusPolicy(QtCore.Qt.StrongFocus)
            self.fullscreen_container.installEventFilter(self.fullscreen_container)
            self.fullscreen_container.destroy_widget.connect(self.toggle_fullscreen)

    def save_image(self, image_type: str):
        self.imagesaver.save_image(image_type)

    def image_saved_callback(self, image_path: str):
        self.image_saved.emit(image_path)

    def start_recording_video(self, video_type: str):
        """"""
        self.videosaver.start_recording_video(video_type)

    def stop_recording_video(self):
        """"""
        self.videosaver.stop_recording_video()

    def play(self, video_format=None):
        if self.pipeline is None:
            self.create_pipeline()

        if self.pipeline.get_state(1000000).state == Gst.State.PLAYING:
            log.debug("Setting pipeline to READY")
            self.pipeline.set_state(Gst.State.NULL)
        if video_format is not None:
            log.info("Setting format to {}".format(video_format))
            caps = self.pipeline.get_by_name("bin")
            caps.set_property("device-caps",
                              Gst.Caps.from_string(video_format))
        log.info("Setting state PLAYING")
        self.pipeline.set_state(Gst.State.PLAYING)

    def new_buffer(self, appsink):
        buf = self.pipeline.get_by_name("sink").emit("pull-sample")
        caps = buf.get_caps()
        struc = caps.get_structure(0)
        b = buf.get_buffer()
        try:
            (ret, buffer_map) = b.map(Gst.MapFlags.READ)
            if self.current_width == 0:
                self.current_width = struc.get_value("width")
            if self.current_height == 0:
                self.current_height = struc.get_value("height")

            # buffer_format = struc.get_value("format")

            self.image = QtGui.QPixmap.fromImage(QtGui.QImage(buffer_map.data,
                                                              struc.get_value("width"),
                                                              struc.get_value("height"),
                                                              QtGui.QImage.Format_ARGB32))
            if self.fullscreen_container is not None:
                self.fullscreen_container.new_pixmap.emit(self.image)
            else:
                self.container.new_pixmap.emit(self.image)

        finally:
            b.unmap(buffer_map)

        return Gst.FlowReturn.OK

    def create_pipeline(self, video_format=None):

        # the queue element before the sink is important.
        # it allows set_state to work as expected.
        # the sink is synced with our main thread (the display thread).
        # changing the state from out main thread will cause a deadlock,
        # since the remaining buffers can not be displayed because our main thread
        # is currently in set_state
        pipeline_str = ("tcambin serial={serial} name=bin "
                        "! video/x-raw,format=BGRx "
                        "! tee name=tee tee. "
                        "! queue "
                        "! videoconvert "
                        "! video/x-raw,format=BGRx "
                        "! appsink name=sink emit-signals=true")

        self.pipeline = None
        self.pipeline = Gst.parse_launch(pipeline_str.format(serial=self.serial))
        self.imagesaver = ImageSaver(self.pipeline, self.serial)
        self.videosaver = VideoSaver(self.pipeline, self.serial)

        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self.new_buffer)
        # Create bus to get events from GStreamer pipeline
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.enable_sync_message_emission()
        self.bus.connect('message::state-changed', self.on_state_changed)
        self.bus.connect('message::error', self.on_error)

        self.data.tcam = self.pipeline.get_by_name("bin")

        self.pipeline.set_state(Gst.State.READY)
        log.debug("Created pipeline and set to READY")

    def pause(self):
        log.info("Setting state to PAUSED")
        self.pipeline.set_state(Gst.State.PAUSED)

    def stop(self):
        log.info("Setting state to NULL")

        self.pipeline.set_state(Gst.State.NULL)
        log.info("Set State to NULL")

    def on_error(self, bus, msg):
        err, dbg = msg.parse_error()

        if msg.src.get_name() == "tcambin-source":
            if err.message == "Device lost":
                log.error("Received device lost message")
                self.fire_device_lost()
            else:
                log.error("Error from source: {}".format(err.message))
        else:
            log.error("ERROR:", msg.src.get_name(), ":", err.message)
            if dbg:
                log.debug("Debug info:", dbg)

    # this function is called when the pipeline changes states.
    # we use it to keep track of the current state
    def on_state_changed(self, bus, msg):
        old, new, pending = msg.parse_state_changed()
        if not msg.src == self.pipeline.get_by_name("bin"):
            # not from the playbin, ignore
            return
        self.state = new

        if new == Gst.State.PLAYING:
            sink = self.pipeline.get_by_name("bin")
            pad = sink.get_static_pad("src")
            caps = pad.query_caps()
            fmt = caps.get_structure(0)
            if fmt is None:
                log.error("Unable to determine format.")
                return

            width = fmt.get_value("width")
            height = fmt.get_value("height")
            # self.container.resize(width, height)

    def get_tcam(self):
        return self.data.tcam

    def create_format_menu(self, parent=None):
        if self.format_menu is not None:
            self.format_menu.clear()
        else:
            self.format_menu = QMenu("Formats",
                                     parent)
        formats = self.get_tcam().get_static_pad("src").query_caps()

        def get_framerates(fmt):
            try:
                rates = fmt.get_value("framerate")
            except TypeError:
                # Workaround for missing GstValueList support in GI
                substr = fmt.to_string()[fmt.to_string().find("framerate="):]
                # try for frame rate lists
                field, values, remain = re.split("{|}", substr, maxsplit=3)
                rates = [x.strip() for x in values.split(",")]
            return rates

        format_dict = {}

        for j in range(formats.get_size()):
            fmt = formats.get_structure(j)
            try:
                # log.info("=== {}".format(fmt.to_string()))
                format_name = fmt.get_name()

                if "ANY" in format_name:
                    continue

                if format_name == "image/jpeg":
                    format_string = format_name
                else:
                    format_string = fmt.get_value("format")
                # ignore additional formats that are generated by the bin
                # we only want the src formats

                # TODO: This will loose GRAY16 formats in lists like { GRAY8, GRAY16_LE, BGRx }
                if ("BGRx" in format_string and "GRAY" not in format_string):
                    format_string = "GRAY8"
                elif ("BGRx" in format_string):
                    continue
                elif (format_string is None or
                      format_string == "None"):
                    continue

                width = fmt.get_value("width")
                height = fmt.get_value("height")

                if format_string not in format_dict:
                    format_dict[format_string] = QMenu(format_string, self)

                f_str = "{} - {}x{}".format(format_string,
                                            width,
                                            height)
                if "None" in f_str:
                    continue
                if "range" in format_string:
                    continue

                res_menu_string = "{}x{}".format(width, height)

                # do not allow entries like [96,2592]x[2,1944]
                if "]x[" in res_menu_string:
                    continue

                f_menu = format_dict[format_string].addMenu(res_menu_string)

            except TypeError as e:
                log.warning("Could not interpret structure. Omitting. {}".format(fmt.to_string()))
                continue

            rates = get_framerates(fmt)
            if rates is None:
                continue
            if type(rates) is Gst.FractionRange:
                continue
            if type(rates) is Gst.Fraction:
                continue
            for rate in rates:
                rate = str(rate)
                action = QAction(rate, self)
                action.setToolTip("Set format to '{}'".format(f_str + "@" + rate))
                if format_string == "image/jpeg":
                    f = "{},,width={},height={},framerate={}".format(format_name,
                                                                     width,
                                                                     height,
                                                                     rate)
                else:
                    f = "{},format={},width={},height={},framerate={}".format(format_name,
                                                                              format_string,
                                                                              width,
                                                                              height,
                                                                              rate)
                # log.debug("Adding '{}'".format(f))
                action.triggered.connect(functools.partial(self.play, f))
                f_menu.addAction(action)

        # do not iterate the dict, but add manually
        # this is neccessary to ensure the order is always correct
        # for key, value in format_dict.items():
        #     self.format_menu.addMenu(value)

        for key, value in format_dict.items():
            self.format_menu.addMenu(value)

    def get_format_menu(self, parent=None):
        """Returns a QMenu which endpoints are connected
        to playing a pipeline with the associated format"""

        if self.format_menu is None:
            if parent is None:
                self.create_format_menu(self)
            else:
                self.create_format_menu(parent)
        return self.format_menu

    def register_device_lost(self, callback):
        self.device_lost_callbacks.append(callback)

    def fire_device_lost(self):
        for cb in self.device_lost_callbacks:
            cb()
