import React from 'react'
import ReactDOM from 'react-dom'

import DebugPanel from './debug_panel'
import ErrorPanel from './error_panel'
import Splash from './splash'
import GreeterPanel from './greeter_panel'
import MainPanel from './main_panel'
import Wizard from './wizard'
import Addressbook from './addressbook'
import ByeSplash from './bye_splash'

import App from 'app'
import 'lib/common'

export default class PanelSwitcher extends React.Component {

  constructor(props) {
    super(props)
    this.state = {
      panel: null,
      panel_properties: null,
      debug: true,
      error: null
    }
    App.switcher = this
  }

  showError(error) {
    this.setState({error: error})
  }

  hideError() {
    this.setState({error: null})
  }

  show(component_name, properties={}) {
    this.setState({panel: component_name, panel_properties: properties})
  }

  render() {
    let elems = []
    if (this.state.error) {
      elems.push(
        <ErrorPanel error={this.state.error} key="error" />
      )
    }
    if (this.panelExist(this.state.panel)) {
      elems.push(
        this.panelRender(this.state.panel, this.state.panel_properties)
      )
    }
    if (this.state.debug && this.state.panel) {
      //window.location.hash = this.state.panel
      //elems.push(
      //  elem(DebugPanel, {key: 'debug'})
      //)
    }
    return <div id="root">{elems}</div>
  }

  panelExist(panel) {
    return panel && this['render_'+panel]
  }

  panelRender(panel_name, props) {
    let panel = this['render_'+panel_name](props)
    return elem('div', {key: 'panel'}, panel)
  }

  render_splash(props)  {return elem(Splash, props)}
  render_wizard(props)  {return elem(Wizard, props)}
  render_greeter(props) {return elem(GreeterPanel, props)}
  render_main(props)    {return elem(MainPanel, props)}
  render_addressbook(props) {return elem(Addressbook, props)}
  render_error(props)   {return elem(ErrorPanel, props)}
  render_bye_splash(props)  {return elem(ByeSplash, props)}

}
