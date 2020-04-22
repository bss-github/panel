import * as p from "@bokehjs/core/properties"

import {div, canvas} from "@bokehjs/core/dom"
import {HTMLBox} from "@bokehjs/models/layouts/html_box"
import {clone} from "@bokehjs/core/util/object"
import {ColorMapper} from "@bokehjs/models/mappers/color_mapper"

import {PanelHTMLBoxView, set_size} from "../layout"
import {vtkns, VolumeType, majorAxis} from "./vtk_utils"
import {VTKColorBar} from "./vtk_colorbar"
import {VTKAxes} from "./vtkaxes"

export abstract class AbstractVTKView extends PanelHTMLBoxView {
  model: AbstractVTKPlot
  protected _axes: any
  protected _camera_callbacks: any[]
  protected _orientationWidget: any
  protected _setting_camera: boolean
  protected _vtk_container: HTMLDivElement
  protected _vtk_renwin: any
  protected _widgetManager: any
  
  initialize(): void {
    super.initialize()
    this._setting_camera = false
    this._camera_callbacks = []
  }

  _add_colorbars(): void {
    //construct colorbars
    const old_info_div = this.el.querySelector(".vtk_info")
    if (old_info_div)
      this.el.removeChild(old_info_div)
    if (this.model.color_mappers.length < 1) return

    const info_div = document.createElement("div")
    const expand_width = "350px"
    const collapsed_width = "30px"
    info_div.classList.add('vtk_info')
    info_div.style.width = expand_width
    info_div.style.padding = "0px 2px 0px 2px"
    info_div.style.maxHeight = "150px"
    info_div.style.height = "auto"
    info_div.style.backgroundColor = "rgba(255, 255, 255, 0.4)"
    info_div.style.borderRadius = "10px"
    info_div.style.margin = "2px"
    info_div.style.boxSizing = "border-box"
    info_div.style.overflow = "hidden"
    info_div.style.overflowY = "auto"
    info_div.style.transition = "width 0.1s linear"
    info_div.style.bottom = "0px"
    info_div.style.position = "absolute"
    this.el.appendChild(info_div)

    //construct colorbars
    const colorbars: VTKColorBar[] = []
    this.model.color_mappers.forEach((mapper) => {
      const cb = new VTKColorBar(info_div, mapper)
      colorbars.push(cb)
    })

    //content when collapsed
    const dots = document.createElement('div');
    dots.style.textAlign = "center"
    dots.style.fontSize = "20px"
    dots.innerText = "..."

    info_div.addEventListener('click', () => {
      if(info_div.style.width === collapsed_width){
        info_div.removeChild(dots)
        info_div.style.height = "auto"
        info_div.style.width = expand_width
        colorbars.forEach((cb) => info_div.appendChild(cb.canvas))
      } else {
        colorbars.forEach((cb) => info_div.removeChild(cb.canvas))
        info_div.style.height = collapsed_width
        info_div.style.width = collapsed_width
        info_div.appendChild(dots)
      }
    })

    info_div.click()
  }

  connect_signals(): void {
    super.connect_signals()
    this.connect(this.model.properties.orientation_widget.change, () => {
      this._orientation_widget_visibility(this.model.orientation_widget)
    })
    this.connect(this.model.properties.camera.change, () =>
      this._set_camera_state()
    )
    this.connect(this.model.properties.axes.change, () => {
      this._delete_axes()
      if (this.model.axes) this._set_axes()
      this._vtk_render()
    })
    this.connect(this.model.properties.color_mappers.change, () => this._add_colorbars())
  }

  render(): void {
    super.render()
    this._orientationWidget = null
    this._vtk_container = div()
    set_size(this._vtk_container, this.model)
    this.el.appendChild(this._vtk_container)
    this._vtk_renwin = vtkns.FullScreenRenderWindow.newInstance({
      rootContainer: this.el,
      container: this._vtk_container,
    })
    this._remove_default_key_binding()
    this._create_orientation_widget()
    this._connect_vtkcamera_to_model()
    this._vtk_renwin.getRenderer().getActiveCamera().modified()
    this._set_camera_state()
    this.model.renderer_el = this._vtk_renwin
  }

  after_layout(): void {
    super.after_layout()
    this._vtk_renwin.resize()
    this._vtk_render()
  }

  invalidate_render(): void {
    this._unsubscribe_camera_cb()
    super.invalidate_render()
  }

  remove(): void {
    this._unsubscribe_camera_cb()
    window.removeEventListener("resize", this._vtk_renwin.resize)
    this._vtk_renwin.delete()
    super.remove()
  }

  get _axes_canvas(): HTMLCanvasElement {
    let axes_canvas = this._vtk_container.querySelector(
      ".axes-canvas"
    ) as HTMLCanvasElement
    if (!axes_canvas) {
      axes_canvas = canvas({
        style: {
          position: "absolute",
          top: "0",
          left: "0",
          width: "100%",
          height: "100%",
        },
      })
      axes_canvas.classList.add("axes-canvas")
      this._vtk_container.appendChild(axes_canvas)
      this._vtk_renwin.setResizeCallback(() => {
        if (this._axes_canvas) {
          const dims = this._vtk_container.getBoundingClientRect()
          const width = Math.floor(dims.width * window.devicePixelRatio)
          const height = Math.floor(dims.height * window.devicePixelRatio)
          this._axes_canvas.setAttribute("width", width.toFixed())
          this._axes_canvas.setAttribute("height", height.toFixed())
        }
      })
    }
    return axes_canvas
  }

  _bind_key_events(): void {
    this.el.addEventListener("mouseenter", () => {
      const interactor = this._vtk_renwin.getInteractor()
      if (this.model.enable_keybindings) {
        document
          .querySelector("body")!
          .addEventListener("keypress", interactor.handleKeyPress)
        document
          .querySelector("body")!
          .addEventListener("keydown", interactor.handleKeyDown)
        document
          .querySelector("body")!
          .addEventListener("keyup", interactor.handleKeyUp)
      }
    })
    this.el.addEventListener("mouseleave", () => {
      const interactor = this._vtk_renwin.getInteractor()
      document
        .querySelector("body")!
        .removeEventListener("keypress", interactor.handleKeyPress)
      document
        .querySelector("body")!
        .removeEventListener("keydown", interactor.handleKeyDown)
      document
        .querySelector("body")!
        .removeEventListener("keyup", interactor.handleKeyUp)
    })
  }

  _connect_vtkcamera_to_model(): void {
    this._camera_callbacks.push(
      this._vtk_renwin
        .getRenderer()
        .getActiveCamera()
        .onModified(
          vtkns.macro.debounce(() => this._get_camera_state(), 50)
        )
    )
  }

  _create_orientation_widget(): void {
    const axes = vtkns.AxesActor.newInstance()

    // add orientation widget
    this._orientationWidget = vtkns.OrientationMarkerWidget.newInstance({
      actor: axes,
      interactor: this._vtk_renwin.getInteractor(),
    })
    this._orientationWidget.setEnabled(true)
    this._orientationWidget.setViewportCorner(
      vtkns.OrientationMarkerWidget.Corners.BOTTOM_RIGHT
    )
    this._orientationWidget.setViewportSize(0.15)
    this._orientationWidget.setMinPixelSize(75)
    this._orientationWidget.setMaxPixelSize(300)

    this._widgetManager = vtkns.WidgetManager.newInstance()
    this._widgetManager.setRenderer(this._orientationWidget.getRenderer())

    const widget = vtkns.InteractiveOrientationWidget.newInstance()
    widget.placeWidget(axes.getBounds())
    widget.setBounds(axes.getBounds())
    widget.setPlaceFactor(1)

    const vw = this._widgetManager.addWidget(widget)

    // Manage user interaction
    vw.onOrientationChange(({direction}: any) => {
      const camera = this._vtk_renwin.getRenderer().getActiveCamera()
      const focalPoint = camera.getFocalPoint()
      const position = camera.getPosition()
      const viewUp = camera.getViewUp()

      const distance = Math.sqrt(
        Math.pow(position[0] - focalPoint[0], 2) +
          Math.pow(position[1] - focalPoint[1], 2) +
          Math.pow(position[2] - focalPoint[2], 2)
      )

      camera.setPosition(
        focalPoint[0] + direction[0] * distance,
        focalPoint[1] + direction[1] * distance,
        focalPoint[2] + direction[2] * distance
      )

      if (direction[0]) camera.setViewUp(majorAxis(viewUp, 1, 2))
      if (direction[1]) camera.setViewUp(majorAxis(viewUp, 0, 2))
      if (direction[2]) camera.setViewUp(majorAxis(viewUp, 0, 1))

      this._vtk_renwin.getRenderer().resetCameraClippingRange()
      this._vtk_render()
    })
    this._orientation_widget_visibility(this.model.orientation_widget)
  }

  _delete_axes(): void {
    if (this._axes) {
      Object.keys(this._axes).forEach((key) =>
        this._vtk_renwin.getRenderer().removeActor(this._axes[key])
      )
      this._axes = null
      const textCtx = this._axes_canvas.getContext("2d")
      if (textCtx)
        textCtx.clearRect(
          0,
          0,
          this._axes_canvas.clientWidth * window.devicePixelRatio,
          this._axes_canvas.clientHeight * window.devicePixelRatio
        )
    }
  }

  _get_camera_state(): void {
    if (!this._setting_camera) {
      this._setting_camera = true
      const state = clone(
        this._vtk_renwin.getRenderer().getActiveCamera().get()
      )
      delete state.classHierarchy
      delete state.vtkObject
      delete state.vtkCamera
      delete state.viewPlaneNormal
      delete state.flattenedDepIds
      delete state.managedInstanceId
      delete state.directionOfProjection
      delete state.projectionMatrix
      delete state.viewMatrix
      delete state.physicalTranslation
      delete state.physicalScale
      delete state.physicalViewUp
      delete state.physicalViewNorth
      delete state.mtime
      this.model.camera = state
      this._setting_camera = false
    }
  }

  _orientation_widget_visibility(visibility: boolean): void {
    this._orientationWidget.setEnabled(visibility)
    if (visibility) this._widgetManager.enablePicking()
    else this._widgetManager.disablePicking()
    this._vtk_render()
  }

  _remove_default_key_binding(): void {
    const interactor = this._vtk_renwin.getInteractor()
    document
      .querySelector("body")!
      .removeEventListener("keypress", interactor.handleKeyPress)
    document
      .querySelector("body")!
      .removeEventListener("keydown", interactor.handleKeyDown)
    document
      .querySelector("body")!
      .removeEventListener("keyup", interactor.handleKeyUp)
  }

  _set_axes(): void {
    if (this.model.axes) {
      const {psActor, axesActor, gridActor} = this.model.axes.create_axes(
        this._axes_canvas
      )
      this._axes = {psActor, axesActor, gridActor}
      this._vtk_renwin.getRenderer().addActor(psActor)
      this._vtk_renwin.getRenderer().addActor(axesActor)
      this._vtk_renwin.getRenderer().addActor(gridActor)
    }
  }

  _set_camera_state(): void {
    if (!this._setting_camera) {
      this._setting_camera = true
      try {
        if (this.model.camera)
          this._vtk_renwin
            .getRenderer()
            .getActiveCamera()
            .set(this.model.camera)
      } finally {
        this._setting_camera = false
      }
      this._vtk_renwin.getRenderer().resetCameraClippingRange()
      this._vtk_render()
    }
  }
  
  _unsubscribe_camera_cb(): void {
    this._camera_callbacks
      .splice(0, this._camera_callbacks.length)
      .map((cb) => cb.unsubscribe())
  }

  _vtk_render(): void {
    if (this._orientationWidget)
      this._orientationWidget.updateMarkerOrientation()
    this._vtk_renwin.getRenderWindow().render()
  }
}

export namespace AbstractVTKPlot {
  export type Attrs = p.AttrsOf<Props>
  export type Props = HTMLBox.Props & {
    axes: p.Property<VTKAxes>
    camera: p.Property<any>
    data: p.Property<string | VolumeType>
    enable_keybindings: p.Property<boolean>
    orientation_widget: p.Property<boolean>
    color_mappers: p.Property<ColorMapper[]>
  }
}

export interface AbstractVTKPlot extends AbstractVTKPlot.Attrs {}

export abstract class AbstractVTKPlot extends HTMLBox {
  properties: AbstractVTKPlot.Props
  renderer_el: any

  static __module__ = "panel.models.vtk"

  constructor(attrs?: Partial<AbstractVTKPlot.Attrs>) {
    super(attrs)
  }

  getActors(): any[] {
    return this.renderer_el.getRenderer().getActors()
  }

  static init_AbstractVTKPlot(): void {
    this.define<AbstractVTKPlot.Props>({
      axes:               [ p.Instance       ],
      camera:             [ p.Instance       ],
      color_mappers:      [ p.Array,      [] ],
      orientation_widget: [ p.Boolean, false ],
    })

    this.override({
      height: 300,
      width: 300,
    })
  }
}
