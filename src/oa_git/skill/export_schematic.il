/* Official-API schematic capture for the oa-git bridge. */

procedure(oagitExportString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitExportMaybeString(value)
  if(value
      then oagitExportString(value)
      else list('jsonNull))
)

procedure(oagitExportScalar(value)
  cond(
    (!value list('jsonNull))
    (eq(value t) list('jsonBool t))
    (stringp(value) value)
    (integerp(value) value)
    (floatp(value) value)
    (t list('jsonNull))
  )
)

procedure(oagitExportScalarP(value paramType)
  cond(
    (equal(paramType "boolean") t)
    (!value nil)
    (stringp(value) t)
    (integerp(value) t)
    (floatp(value) t)
    (t nil)
  )
)

procedure(oagitExportCdfValuePresentP(value paramType)
  if(equal(paramType "boolean")
      then t
      else value)
)

procedure(oagitExportBool(value)
  list('jsonBool if(eq(value t) t nil))
)

procedure(oagitExportObject(pairs)
  cons('jsonObject pairs)
)

procedure(oagitExportArray(items)
  cons('jsonArray items)
)

procedure(oagitExportNativeValue(value)
  cond(
    (listp(value) oagitExportNativeList(value))
    (eq(value t) list('jsonBool t))
    (stringp(value) value)
    (integerp(value) value)
    (floatp(value) value)
    (t oagitExportString(value))
  )
)

procedure(oagitExportNativeList(values)
  let((out item tail)
    out = nil
    if(values && equal(car(values) nil)
        then
          tail = cdr(values)
          while(tail
            when(cdr(tail)
              out = cons(list(oagitExportString(car(tail))
                              oagitExportNativeValue(cadr(tail))) out))
            tail = cddr(tail)
          )
          oagitExportObject(reverse(out))
        else
          foreach(item values
            out = cons(oagitExportNativeValue(item) out))
          oagitExportArray(reverse(out)))
  )
)

procedure(oagitExportOptionalNativeField(cv field)
  let((result value)
    result = if(equal(field "constraints")
                then errset(cv~>constraints t)
                else errset(cv~>sheet t))
    if(result && car(result)
        then
          value = car(result)
          if(listp(value) && equal(car(value) nil)
              then oagitExportNativeValue(value)
              else oagitExportObject(
                     list(list("value" oagitExportNativeValue(value)))))
        else list('jsonObject))
  )
)

procedure(oagitExportPointObject(point)
  let((xResult yResult x y)
    xResult = errset(point~>x t)
    yResult = errset(point~>y t)
    if(xResult && yResult && numberp(car(xResult)) && numberp(car(yResult))
        then
          x = car(xResult)
          y = car(yResult)
          list('jsonArray oagitExportScalar(x) oagitExportScalar(y))
        else list('jsonArray))
  )
)

procedure(oagitExportPoint(point)
  cond(
    (point && listp(point) && cadr(point) != nil
      list('jsonArray oagitExportScalar(car(point))
                           oagitExportScalar(cadr(point))))
    (point oagitExportPointObject(point))
    (t list('jsonArray))
  )
)

procedure(oagitExportPoints(points)
  let((out index maxIdx)
    out = nil
    if(points && numberp(car(points))
        then
          index = 0
          maxIdx = length(points) - 2
          while(index <= maxIdx
            out = cons(oagitExportPoint(
                         list(nth(index points) nth(index + 1 points))) out)
            index = index + 2
          )
        else
          foreach(point points out = cons(oagitExportPoint(point) out)))
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportFindProp(props name)
  let((found)
    found = nil
    foreach(prop props
      when(equal(oagitExportString(prop~>name) oagitExportString(name))
        found = prop
      )
    )
    found
  )
)

procedure(oagitExportParamType(param)
  let((paramType)
    paramType = param~>paramType
    unless(paramType paramType = param~>type)
    oagitExportString(paramType)
  )
)

procedure(oagitExportPropertyValue(prop)
  let((value propType)
    value = prop~>value
    propType = oagitExportString(prop~>valueType)
    cond(
      (equal(propType "boolean") oagitExportBool(value))
      (equal(propType "int")
        if(numberp(value) fix(value) oagitExportScalar(value)))
      (equal(propType "float")
        if(numberp(value) float(value) oagitExportScalar(value)))
      (equal(propType "string") oagitExportString(value))
      (t oagitExportScalar(value))
    )
  )
)

procedure(oagitExportPropertyPresentP(prop)
  let((value propType)
    value = prop~>value
    propType = oagitExportString(prop~>valueType)
    if(equal(propType "boolean")
        then t
        else value)
  )
)

procedure(oagitExportCdfValue(value paramType)
  cond(
    (equal(paramType "boolean") oagitExportBool(value))
    (equal(paramType "string") if(stringp(value) value nil))
    (equal(paramType "cyclic") if(stringp(value) value nil))
    (equal(paramType "radio") if(stringp(value) value nil))
    (equal(paramType "int")
      if(numberp(value)
          if(integerp(value) value fix(value))
          nil))
    (equal(paramType "float")
      if(numberp(value) float(value) nil))
    (t nil)
  )
)

procedure(oagitExportIgnoredCdfParamP(pName)
  member(oagitExportString(pName) list(
    "lxComponentType"
    "description"
    "pdkVersion"
    "macroArgumentStyle"
    "pasUpdateParamList"
    "lxIgnoredParamsForCAS"
    "modelLabelSet"
    "opPointLabelSet"
    "paramLabelSet"
    "lxStopList"
    "lxUseCell"
  ))
)

procedure(oagitExportInvokeCdfHook(procName cdfData)
  let((procStr sym)
    procStr = if(stringp(procName) procName
                 if(symbolp(procName) sprintf(nil "%s" procName) ""))
    if(strlen(procStr) > 0
        then
          if(index(procStr "(")
              then errset(progn(evalstring(procStr) t) t)
              else
                sym = stringToSymbol(procStr)
                if(isCallable(sym)
                    then errset(progn(funcall(sym cdfData) t) t)
                    else nil))
        else list(t))
  )
)

procedure(oagitExportEditableEvalP(editableExpr inst)
  let((cvType res)
    cond(
      (equal(editableExpr "nil") nil)
      (!editableExpr || eq(editableExpr t) ||
       equal(editableExpr "t") || equal(editableExpr "")
        t)
      (stringp(editableExpr)
        cvType = nil
        when(inst && inst~>cellView
          cvType = oagitExportString(inst~>cellView~>cellViewType)
        )
        cond(
          (equal(editableExpr "iPDK_isSchematic()")
            equal(cvType "schematic"))
          (rexMatchp("deGetEditCellView" editableExpr)
            cond(
              (rexMatchp("!=.*\"maskLayout\"" editableExpr) ||
               rexMatchp("==.*\"schematic\"" editableExpr)
                equal(cvType "schematic"))
              (rexMatchp("==.*\"maskLayout\"" editableExpr)
                if(equal(cvType "schematic") nil t))
              (t
                res = errset(evalstring(editableExpr) t)
                if(res && car(res) t nil))
            ))
          (t
            res = errset(evalstring(editableExpr) t)
            if(res && car(res) t nil))
        ))
      (t nil)
    )
  )
)

procedure(oagitExportCdfData(inst)
  let((instCdf baseCdf baseCell params paramTypes paramOrder derived props
       cdfParam baseParam pName paramType editable editableOk stored current
       defaultValue writable differs encodedVal formInit)
    params = nil
    paramTypes = nil
    paramOrder = nil
    derived = nil
    props = nil
    instCdf = cdfGetInstCDF(inst)
    baseCdf = nil
    baseCell = ddGetObj(inst~>libName inst~>cellName)
    when(baseCell baseCdf = cdfGetBaseCellCDF(baseCell))
    when(instCdf
      cdfgData = instCdf
      cdfgForm = nil
      formInit = (instCdf && instCdf~>formInitProc) ||
                 (baseCdf && baseCdf~>formInitProc)
      when(formInit && !equal(formInit "")
        errset(oagitExportInvokeCdfHook(formInit instCdf) t)
      )
      foreach(cdfParam instCdf~>parameters
        pName = oagitExportString(cdfParam~>name)
        unless(oagitExportIgnoredCdfParamP(pName)
          paramType = oagitExportParamType(cdfParam)
          editable = cdfParam~>editable
          stored = oagitExportFindProp(inst~>prop pName)
          current = cdfParam~>value
          baseParam = nil
          when(baseCdf baseParam = cdfFindParamByName(baseCdf pName))
          defaultValue = nil
          when(baseParam
            defaultValue = baseParam~>defValue
          )
          writable = nil
          when(baseParam &&
               member(paramType list("string" "int" "float" "boolean" "cyclic" "radio"))
            editableOk = oagitExportEditableEvalP(editable inst)
            when(editableOk && !equal(editable baseParam~>editable)
              editableOk = oagitExportEditableEvalP(baseParam~>editable inst)
            )
            writable = editableOk
          )
          differs = !equal(current defaultValue)
          encodedVal = oagitExportCdfValue(current paramType)
          cond(
            (writable && (differs || stored) &&
             oagitExportCdfValuePresentP(current paramType) &&
             encodedVal != nil
              params = cons(list(pName encodedVal) params)
              paramTypes = cons(list(pName paramType) paramTypes)
              paramOrder = cons(pName paramOrder))
            (!writable && (differs || stored || equal(pName "model")) &&
             oagitExportScalarP(current paramType) &&
             oagitExportCdfValuePresentP(current paramType) &&
             encodedVal != nil
              derived = cons(list(pName encodedVal) derived))
          )
        )
      )
    )
    foreach(prop inst~>prop
      pName = prop~>name
      cdfParam = nil
      when(instCdf cdfParam = cdfFindParamByName(instCdf pName))
      when(!cdfParam && oagitExportPropertyPresentP(prop)
        props = cons(list(oagitExportString(pName)
                          oagitExportPropertyValue(prop)) props)
      )
    )
    list(reverse(params) reverse(paramTypes) reverse(derived)
         reverse(props) reverse(paramOrder))
  )
)

procedure(oagitExportConnections(inst)
  let((pairs)
    pairs = nil
    foreach(instTerm inst~>instTerms
      when(instTerm~>net && instTerm~>net~>name &&
           strlen(oagitExportString(instTerm~>net~>name)) > 0
        pairs = cons(list(oagitExportString(instTerm~>name)
                          oagitExportString(instTerm~>net~>name)) pairs)
      )
    )
    oagitExportObject(reverse(pairs))
  )
)

procedure(oagitExportPorts(terminals)
  let((out)
    out = nil
    foreach(term terminals
      out = cons(
        list('jsonObject
             list("name" oagitExportString(term~>name))
             list("direction"
                  if(term~>direction
                      oagitExportString(term~>direction)
                      "inputOutput")))
        out)
    )
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportNets(nets)
  let((out)
    out = nil
    foreach(net nets
      out = cons(
        list('jsonObject
             list("name" oagitExportString(net~>name))
             list("is_global" oagitExportBool(net~>isGlobal))
             list("num_bits"
                  if(net~>numBits
                      if(integerp(net~>numBits) net~>numBits fix(net~>numBits))
                      1)))
        out)
    )
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportPortOrder(terminals)
  let((out)
    out = nil
    foreach(term terminals
      out = cons(oagitExportString(term~>name) out)
    )
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportPins(cv pinFigIds)
  let((out fig master netName)
    out = nil
    foreach(term cv~>terminals
      foreach(pin term~>pins
        fig = pin~>fig
        when(fig
          pinFigIds[fig] = t
          master = fig~>master
          netName = nil
          when(term~>net netName = term~>net~>name)
          out = cons(
            list('jsonObject
                 list("name" oagitExportString(term~>name))
                 list("net" oagitExportString(netName))
                 list("direction"
                      if(term~>direction
                          oagitExportString(term~>direction)
                          "inputOutput"))
                 list("lib" oagitExportString(master~>libName))
                 list("cell" oagitExportString(master~>cellName))
                 list("view" if(master~>viewName
                                   oagitExportString(master~>viewName)
                                   "symbol"))
                 list("xy" oagitExportPoint(fig~>xy))
                 list("orient" if(fig~>orient oagitExportString(fig~>orient) "R0")))
            out)
        )
      )
    )
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportInstance(inst pinFigIds)
  let((cdfData params paramTypes derived props paramOrder)
    cdfData = oagitExportCdfData(inst)
    params = car(cdfData)
    paramTypes = cadr(cdfData)
    derived = caddr(cdfData)
    props = cadddr(cdfData)
    paramOrder = nth(4 cdfData)
    list('jsonObject
         list("name" oagitExportString(inst~>name))
         list("lib" oagitExportString(inst~>libName))
         list("cell" oagitExportString(inst~>cellName))
         list("view" if(inst~>viewName oagitExportString(inst~>viewName) "symbol"))
         list("xy" oagitExportPoint(inst~>xy))
         list("orient" if(inst~>orient oagitExportString(inst~>orient) "R0"))
         list("connections" oagitExportConnections(inst))
         list("params" oagitExportObject(params))
         list("param_types" oagitExportObject(paramTypes))
         list("param_order" oagitExportArray(paramOrder))
         list("derived_params" oagitExportObject(derived))
         list("props" oagitExportObject(props)))
  )
)

procedure(oagitExportInstances(cv pinFigIds)
  let((out)
    out = nil
    foreach(inst cv~>instances
      unless(pinFigIds[inst]
        out = cons(oagitExportInstance(inst pinFigIds) out)
      )
    )
    oagitExportArray(reverse(out))
  )
)

procedure(oagitExportIsLineOrPath(shape)
  equal(shape~>objType "line") || equal(shape~>objType "path")
)

procedure(oagitExportIsWireShape(shape)
  oagitExportIsLineOrPath(shape) && equal(shape~>layerName "wire")
)

procedure(oagitExportLabel(label)
  list('jsonObject
       list("text" oagitExportString(label~>theLabel))
       list("xy" oagitExportPoint(label~>xy))
       list("justify"
            if(label~>justify oagitExportString(label~>justify) "centerCenter"))
       list("orient" if(label~>orient oagitExportString(label~>orient) "R0"))
       list("font" if(label~>font oagitExportString(label~>font) "stick"))
       list("height" if(label~>height label~>height 0.0625)))
)

procedure(oagitExportNoteLabel(label)
  list('jsonObject
       list("text" oagitExportString(label~>theLabel))
       list("xy" oagitExportPoint(label~>xy))
       list("layer"
            if(label~>layerName oagitExportString(label~>layerName) "text"))
       list("purpose"
            if(label~>purpose oagitExportString(label~>purpose) "drawing"))
       list("justify"
            if(label~>justify oagitExportString(label~>justify) "lowerLeft"))
       list("orient" if(label~>orient oagitExportString(label~>orient) "R0"))
       list("font" if(label~>font oagitExportString(label~>font) "stick"))
       list("height" if(label~>height label~>height 0.0625)))
)

procedure(oagitExportWire(shape)
  let((labels netName)
    labels = nil
    foreach(child shape~>children
      when(equal(child~>objType "label")
        labels = cons(oagitExportLabel(child) labels)
      )
    )
    netName = nil
    when(shape~>net netName = shape~>net~>name)
    list('jsonObject
         list("points" oagitExportPoints(shape~>points))
         list("net" oagitExportMaybeString(netName))
         list("width" if(shape~>width shape~>width 0.0))
         list("route_method" "full")
         list("labels" oagitExportArray(reverse(labels))))
  )
)

procedure(oagitExportOmission(shape reason)
  list('jsonObject
       list("kind" "shape")
       list("identifier" oagitExportString(shape~>objType))
       list("reason" reason)
       list("details"
            list('jsonObject
                 list("obj_type" oagitExportString(shape~>objType))
                 list("layer" oagitExportMaybeString(shape~>layerName))))
  )
)

procedure(oagitExportIsSolderDot(shape)
  equal(shape~>objType "ellipse") && equal(shape~>layerName "wire")
)

procedure(oagitExportIsAttachedTextDisplay(shape)
  equal(shape~>objType "textDisplay") && (shape~>parent || shape~>associate)
)

procedure(oagitExportIsAnnotationShape(shape)
  member(shape~>objType list("rect" "polygon" "line"))
)

procedure(oagitExportAnnotation(shape)
  let((kind pts width layer purpose)
    kind = oagitExportString(shape~>objType)
    pts = if(equal(kind "rect")
              then oagitExportPoints(shape~>bBox)
              else oagitExportPoints(shape~>points))
    width = if(equal(kind "line") && shape~>width && numberp(shape~>width)
                then float(shape~>width)
                else 0.0)
    layer = if(shape~>layerName oagitExportString(shape~>layerName) "text")
    purpose = if(shape~>purpose oagitExportString(shape~>purpose) "drawing")
    list('jsonObject
         list("kind" kind)
         list("layer" layer)
         list("purpose" purpose)
         list("points" pts)
         list("width" width))
  )
)

procedure(oagitExportGeometry(cv)
  let((wires labels omissions annotations shape parent)
    wires = nil
    labels = nil
    omissions = nil
    annotations = nil
    foreach(shape cv~>shapes
      cond(
        (oagitExportIsWireShape(shape)
          wires = cons(oagitExportWire(shape) wires))
        (oagitExportIsSolderDot(shape)
          /* Virtuoso schCreateWire/schCheck regenerates wire-layer solder dots. */
          nil)
        (oagitExportIsAttachedTextDisplay(shape)
          /* Virtuoso schCreatePin/schCreateInst regenerates attached textDisplay labels. */
          nil)
        (oagitExportIsAnnotationShape(shape)
          annotations = cons(oagitExportAnnotation(shape) annotations))
        (oagitExportIsLineOrPath(shape)
          omissions = cons(oagitExportOmission(shape "unsupported_non_wire_shape") omissions))
        (equal(shape~>objType "label")
          parent = shape~>parent
          unless(parent && oagitExportIsWireShape(parent)
            labels = cons(oagitExportNoteLabel(shape) labels)
          ))
        (t omissions = cons(oagitExportOmission(shape "unsupported_shape") omissions))
      )
    )
    list(reverse(wires) reverse(labels) reverse(omissions) reverse(annotations))
  )
)

procedure(oagitExportInternalCellPropP(name)
  member(name list(
    "lastSchematicExtraction"
    "schLastCheckTime"
    "schGeometryLastUpdated"
    "schGeometryVersion"
    "schVersion"
    "connUpdateTime"
    "connectivityLastUpdated"
    "connectivityLastChanged"
    "pin#"
    "inst#"
    "net#"
    "viewSubType"
    "instancesLastUpdated"
    "instancesLastChanged"
    "instancesLastModified"
    "interfaceLastChanged"
    "schLastExtracted"
  ))
)

procedure(oagitExportPropOmission(name propType)
  list('jsonObject
       list("kind" "property")
       list("identifier" oagitExportString(name))
       list("reason" "unsupported_property_type")
       list("details"
            list('jsonObject
                 list("value_type" oagitExportString(propType)))))
)

procedure(oagitExportCellProperties(props)
  let((out propOmissions name propType)
    out = nil
    propOmissions = nil
    foreach(prop props
      name = oagitExportString(prop~>name)
      propType = oagitExportString(prop~>valueType)
      unless(oagitExportInternalCellPropP(name)
        cond(
          (member(propType list("string" "int" "float" "boolean"))
            when(oagitExportPropertyPresentP(prop)
              out = cons(list(name oagitExportPropertyValue(prop)) out)))
          (t
            propOmissions = cons(oagitExportPropOmission(name propType) propOmissions))
        )
      )
    )
    list(oagitExportObject(reverse(out)) reverse(propOmissions))
  )
)

procedure(oagitExportClose(cv openedByScript)
  when(cv && openedByScript
    errset(dbClose(cv) t)
  )
)

procedure(oagitExportBuildResponse(cv lib cell view)
  let((pinFigIds geometry propResult cellProps allOmissions terminals nets metadata pins instances)
    pinFigIds = makeTable("oagitPinFigIds" nil)
    terminals = cv~>terminals
    nets = cv~>nets
    pins = oagitExportPins(cv pinFigIds)
    instances = oagitExportInstances(cv pinFigIds)
    geometry = oagitExportGeometry(cv)
    propResult = oagitExportCellProperties(cv~>prop)
    cellProps = car(propResult)
    allOmissions = append(caddr(geometry) cadr(propResult))
    metadata = list('jsonObject
                    list("cellview_type" "schematic")
                    list("dbu_per_uu" oagitExportScalar(cv~>DBUPerUU))
                    list("port_order" oagitExportPortOrder(terminals)))
    list('jsonObject
         list("ok" list('jsonBool t))
         list("error" list('jsonNull))
         list("cellview"
              list('jsonObject
                   list("library" oagitExportString(lib))
                   list("cell" oagitExportString(cell))
                   list("view" oagitExportString(view))))
         list("modified_but_not_saved" list('jsonBool nil))
         list("metadata" metadata)
         list("ports" oagitExportPorts(terminals))
         list("nets" oagitExportNets(nets))
         list("instances" instances)
         list("pins" pins)
         list("wires" oagitExportArray(car(geometry)))
         list("labels" oagitExportArray(cadr(geometry)))
         list("annotations" oagitExportArray(cadddr(geometry)))
         list("properties" cellProps)
         list("constraints" oagitExportOptionalNativeField(cv "constraints"))
         list("sheet" oagitExportOptionalNativeField(cv "sheet"))
         list("omissions" oagitExportArray(allOmissions)))
  )
)

procedure(oagitRunExport(req)
  let((outPath lib cell view cv openedByScript captureResult response)
    outPath = req->output_path
    lib = req->library
    cell = req->cell
    view = req->view
    cv = dbFindOpenCellViewByName(lib cell view)
    if(cv
        then openedByScript = nil
        else
          cv = dbOpenCellViewByType(lib cell view nil "r")
          openedByScript = t)
    cond(
      (!cv
        oagitFail(outPath "open_failed" "unable to open requested CellView"))
      (cv~>modifiedButNotSaved
        oagitExportClose(cv openedByScript)
        oagitFail(outPath "modified_but_not_saved"
                  "CellView has unsaved modifications"))
      (t
        captureResult = errset(
          oagitExportBuildResponse(cv lib cell view)
          t)
        if(captureResult
            then
              response = car(captureResult)
              errset(oagitWriteJsonFile(outPath response) t)
              oagitExportClose(cv openedByScript)
              response
            else
              oagitExportClose(cv openedByScript)
              oagitFail(outPath "capture_failed"
                        sprintf(nil "unexpected capture error: %L" errset.errset))))
    )
  )
)

unless(isCallable('oagitJsonEncode)
  let((commonPath)
    commonPath = getShellEnvVar("OAGIT_COMMON_IL")
    when(commonPath load(commonPath))
  )
)

unless(boundp('oagitRequest)
  let((requestPath)
    requestPath = getShellEnvVar("OAGIT_REQUEST_IL")
    when(requestPath load(requestPath))
  )
)

when(boundp('oagitRequest) &&
     oagitRequest->action == "capture_schematic"
  oagitRunExport(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
