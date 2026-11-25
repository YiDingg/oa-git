/* Official-API symbol capture for the oa-git bridge (IC26.1 schematicSymbol). */

procedure(oagitSymExpString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitSymExpMaybeString(value)
  if(value
      then oagitSymExpString(value)
      else list('jsonNull))
)

procedure(oagitSymExpScalar(value)
  cond(
    (!value list('jsonNull))
    (eq(value t) list('jsonBool t))
    (stringp(value) value)
    (integerp(value) value)
    (floatp(value) value)
    (t list('jsonNull))
  )
)

procedure(oagitSymExpBool(value)
  list('jsonBool if(eq(value t) t nil))
)

procedure(oagitSymExpObject(pairs)
  cons('jsonObject pairs)
)

procedure(oagitSymExpArray(items)
  cons('jsonArray items)
)

procedure(oagitSymExpPoint(point)
  let((x y)
    if(point && listp(point) && cadr(point) != nil
        then
          x = car(point)
          y = cadr(point)
          list('jsonArray float(x) float(y))
        else list('jsonArray 0.0 0.0))
  )
)

procedure(oagitSymExpBBox(bbox)
  if(bbox && listp(bbox) && cadr(bbox) != nil
      then list('jsonArray oagitSymExpPoint(car(bbox))
                           oagitSymExpPoint(cadr(bbox)))
      else list('jsonNull))
)

procedure(oagitSymExpBBoxCenter(bbox)
  let((p0 p1 cx cy)
    if(bbox && listp(bbox) && cadr(bbox) != nil
        then
          p0 = car(bbox)
          p1 = cadr(bbox)
          cx = (float(car(p0)) + float(car(p1))) / 2.0
          cy = (float(cadr(p0)) + float(cadr(p1))) / 2.0
          list('jsonArray cx cy)
        else list('jsonArray 0.0 0.0))
  )
)

procedure(oagitSymExpPoints(points)
  let((out idx maxIdx)
    out = nil
    if(points && numberp(car(points))
        then
          idx = 0
          maxIdx = length(points) - 2
          while(idx <= maxIdx
            out = cons(oagitSymExpPoint(
                         list(nth(idx points) nth(idx + 1 points))) out)
            idx = idx + 2
          )
        else
          foreach(pt points
            out = cons(oagitSymExpPoint(pt) out)))
    oagitSymExpArray(reverse(out))
  )
)

procedure(oagitSymExpOmission(kind identifier reason detailsPairs)
  list('jsonObject
       list("kind" oagitSymExpString(kind))
       list("identifier" oagitSymExpString(identifier))
       list("reason" oagitSymExpString(reason))
       list("details" oagitSymExpObject(detailsPairs)))
)

procedure(oagitSymExpPinRecord(term fig)
  let((figType layer purpose dir orient master xy figPairs)
    figType = oagitSymExpString(fig~>objType)
    dir = if(term~>direction oagitSymExpString(term~>direction) "inputOutput")
    orient = if(fig~>orient oagitSymExpString(fig~>orient) "R0")
    layer = if(fig~>layerName oagitSymExpString(fig~>layerName) "pin")
    purpose = if(fig~>purpose oagitSymExpString(fig~>purpose) "drawing")
    cond(
      (equal(figType "rect")
        xy = oagitSymExpBBoxCenter(fig~>bBox)
        figPairs = list(
          list("kind" "rect")
          list("layer" layer)
          list("purpose" purpose)
          list("bbox" oagitSymExpBBox(fig~>bBox))))
      (equal(figType "ellipse") || equal(figType "circle")
        xy = oagitSymExpBBoxCenter(fig~>bBox)
        figPairs = list(
          list("kind" figType)
          list("layer" layer)
          list("purpose" purpose)
          list("bbox" oagitSymExpBBox(fig~>bBox))))
      (equal(figType "polygon")
        xy = oagitSymExpBBoxCenter(fig~>bBox)
        figPairs = list(
          list("kind" "polygon")
          list("layer" layer)
          list("purpose" purpose)
          list("bbox" oagitSymExpBBox(fig~>bBox))
          list("points" oagitSymExpPoints(fig~>points))))
      (equal(figType "inst")
        master = fig~>master
        xy = oagitSymExpPoint(fig~>xy)
        figPairs = list(
          list("kind" "inst")
          list("layer" "pin")
          list("purpose" "drawing")
          list("lib" oagitSymExpString(master~>libName))
          list("cell" oagitSymExpString(master~>cellName))
          list("view" if(master~>viewName oagitSymExpString(master~>viewName) "symbol"))
          list("bbox" if(fig~>bBox oagitSymExpBBox(fig~>bBox) list('jsonNull)))))
      (t
        figPairs = nil)
    )
    if(figPairs
        then
          list('jsonObject
               list("name" oagitSymExpString(term~>name))
               list("direction" dir)
               list("xy" xy)
               list("orient" orient)
               list("pin_figure" oagitSymExpObject(figPairs)))
        else nil)
  )
)

procedure(oagitSymExpPins(cv pinFigIds)
  let((out omissions termPins primaryFig fig rec termName)
    out = nil
    omissions = nil
    foreach(term cv~>terminals
      termName = oagitSymExpString(term~>name)
      termPins = term~>pins
      primaryFig = nil
      foreach(pin termPins
        fig = pin~>fig
        when(fig
          pinFigIds[fig] = t
          if(!primaryFig
              then primaryFig = fig
              else
                omissions = cons(
                  oagitSymExpOmission(
                    "pin_figure" termName "duplicate_pin_figure"
                    list(list("obj_type" oagitSymExpString(fig~>objType))))
                  omissions))
        )
      )
      if(!primaryFig
          then
            omissions = cons(
              oagitSymExpOmission(
                "terminal" termName "terminal_without_pin_figure" nil)
              omissions)
          else
            rec = oagitSymExpPinRecord(term primaryFig)
            if(rec
                then out = cons(rec out)
                else
                  omissions = cons(
                    oagitSymExpOmission(
                      "pin_figure"
                      oagitSymExpString(primaryFig~>objType)
                      "unsupported_pin_figure"
                      list(list("terminal" termName)
                           list("obj_type" oagitSymExpString(primaryFig~>objType))))
                    omissions)))
    )
    list(oagitSymExpArray(reverse(out)) reverse(omissions))
  )
)

procedure(oagitSymExpLabelRecord(shape text labelType)
  list('jsonObject
       list("text" oagitSymExpString(text))
       list("xy" oagitSymExpPoint(shape~>xy))
       list("layer" if(shape~>layerName oagitSymExpString(shape~>layerName) "pin"))
       list("purpose" if(shape~>purpose oagitSymExpString(shape~>purpose) "label"))
       list("justify" if(shape~>justify oagitSymExpString(shape~>justify) "centerCenter"))
       list("orient" if(shape~>orient oagitSymExpString(shape~>orient) "R0"))
       list("font" if(shape~>font oagitSymExpString(shape~>font) "stick"))
       list("height" if(numberp(shape~>height) float(shape~>height) 0.0625))
       list("label_type" labelType))
)

procedure(oagitSymExpTextDisplayText(shape)
  let((res text)
    text = nil
    res = errset(shape~>theLabel t)
    when(res && stringp(car(res)) && strlen(car(res)) > 0
      text = car(res))
    unless(text
      res = errset(shape~>name t)
      when(res && stringp(car(res)) && strlen(car(res)) > 0
        text = car(res)))
    text
  )
)

procedure(oagitSymExpGraphics(cv pinFigIds)
  let((shapes labels nlpLabels ilLabels omissions selectionBox
       objType layer purpose lType tdText inst)
    shapes = nil
    labels = nil
    nlpLabels = nil
    ilLabels = nil
    omissions = nil
    selectionBox = nil
    foreach(shape cv~>shapes
      unless(pinFigIds[shape]
        objType = oagitSymExpString(shape~>objType)
        layer = if(shape~>layerName oagitSymExpString(shape~>layerName) "")
        purpose = if(shape~>purpose oagitSymExpString(shape~>purpose) "drawing")
        cond(
          (equal(objType "rect") && equal(layer "instance") && equal(purpose "drawing")
            if(!selectionBox
                then selectionBox = oagitSymExpBBox(shape~>bBox)
                else
                  omissions = cons(
                    oagitSymExpOmission(
                      "shape" "rect" "duplicate_selection_box"
                      list(list("layer" layer) list("purpose" purpose)))
                    omissions)))
          (equal(objType "line")
            shapes = cons(
              list('jsonObject
                   list("kind" "line")
                   list("layer" layer)
                   list("purpose" purpose)
                   list("points" oagitSymExpPoints(shape~>points))
                   list("width" 0.0)
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "rect")
            shapes = cons(
              list('jsonObject
                   list("kind" "rect")
                   list("layer" layer)
                   list("purpose" purpose)
                   list("bbox" oagitSymExpBBox(shape~>bBox))
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "polygon")
            shapes = cons(
              list('jsonObject
                   list("kind" "polygon")
                   list("layer" layer)
                   list("purpose" purpose)
                   list("points" oagitSymExpPoints(shape~>points))
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "path")
            shapes = cons(
              list('jsonObject
                   list("kind" "path")
                   list("layer" layer)
                   list("purpose" purpose)
                   list("points" oagitSymExpPoints(shape~>points))
                   list("width" if(numberp(shape~>width) float(shape~>width) 0.0))
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "arc")
            shapes = cons(
              list('jsonObject
                   list("kind" "arc")
                   list("layer" layer)
                   list("purpose" purpose)
                   list("arc_bbox" oagitSymExpBBox(if(shape~>arcBBox shape~>arcBBox shape~>bBox)))
                   list("ellipse_bbox" oagitSymExpBBox(shape~>ellipseBBox))
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "ellipse") || equal(objType "circle")
            shapes = cons(
              list('jsonObject
                   list("kind" objType)
                   list("layer" layer)
                   list("purpose" purpose)
                   list("bbox" oagitSymExpBBox(shape~>bBox))
                   list("fill" list('jsonBool nil)))
              shapes))
          (equal(objType "label")
            lType = oagitSymExpString(shape~>labelType)
            cond(
              (equal(lType "NLPLabel")
                nlpLabels = cons(
                  oagitSymExpLabelRecord(shape shape~>theLabel "NLPLabel")
                  nlpLabels))
              (equal(lType "ILLabel")
                ilLabels = cons(
                  oagitSymExpLabelRecord(shape shape~>theLabel "ILLabel")
                  ilLabels))
              (equal(lType "normalLabel") || equal(lType "")
                labels = cons(
                  oagitSymExpLabelRecord(shape shape~>theLabel "normalLabel")
                  labels))
              (t
                omissions = cons(
                  oagitSymExpOmission(
                    "label" lType "unsupported_label_type"
                    list(list("layer" layer) list("purpose" purpose)))
                  omissions))
            ))
          (equal(objType "textDisplay")
            tdText = oagitSymExpTextDisplayText(shape)
            lType = oagitSymExpString(shape~>labelType)
            if(tdText
                then
                  cond(
                    (equal(lType "NLPLabel")
                      nlpLabels = cons(
                        oagitSymExpLabelRecord(shape tdText "NLPLabel")
                        nlpLabels))
                    (equal(lType "normalLabel")
                      labels = cons(
                        oagitSymExpLabelRecord(shape tdText "normalLabel")
                        labels))
                    (t
                      ilLabels = cons(
                        oagitSymExpLabelRecord(shape tdText "ILLabel")
                        ilLabels))
                  )
                else
                  omissions = cons(
                    oagitSymExpOmission(
                      "shape" "textDisplay" "unsupported_text_display"
                      list(list("layer" layer) list("purpose" purpose)))
                    omissions)))
          (t
            omissions = cons(
              oagitSymExpOmission(
                "shape" objType "unsupported_shape"
                list(list("obj_type" objType)
                     list("layer" layer)
                     list("purpose" purpose)))
              omissions))
        )
      )
    )
    foreach(inst cv~>instances
      unless(pinFigIds[inst]
        omissions = cons(
          oagitSymExpOmission(
            "instance" oagitSymExpString(inst~>name) "unsupported_symbol_instance"
            list(list("lib" oagitSymExpMaybeString(inst~>libName))
                 list("cell" oagitSymExpMaybeString(inst~>cellName))))
          omissions)
      )
    )
    list(
      list('jsonObject
           list("shapes" oagitSymExpArray(reverse(shapes)))
           list("labels" oagitSymExpArray(reverse(labels)))
           list("nlp_labels" oagitSymExpArray(reverse(nlpLabels)))
           list("il_labels" oagitSymExpArray(reverse(ilLabels)))
           list("selection_box" if(selectionBox selectionBox list('jsonNull)))
           list("origin" list('jsonArray 0.0 0.0))
           list("bbox" if(cv~>bBox oagitSymExpBBox(cv~>bBox) list('jsonNull))))
      reverse(omissions)
    )
  )
)

procedure(oagitSymExpInternalPropP(name)
  member(name list(
    "pin#"
    "inst#"
    "net#"
    "viewSubType"
    "interfaceLastChanged"
    "instancesLastChanged"
    "instancesLastUpdated"
    "lastSchematicExtraction"
    "schLastCheckTime"
    "schGeometryLastUpdated"
    "schGeometryVersion"
    "schVersion"
    "connUpdateTime"
    "connectivityLastUpdated"
  )) ||
  rexMatchp("LastChanged$" name) ||
  rexMatchp("LastUpdated$" name)
)

procedure(oagitSymExpPropValue(prop)
  let((val pType)
    val = prop~>value
    pType = oagitSymExpString(prop~>valueType)
    cond(
      (equal(pType "boolean") oagitSymExpBool(val))
      (equal(pType "int") if(numberp(val) fix(val) oagitSymExpScalar(val)))
      (equal(pType "float") if(numberp(val) float(val) oagitSymExpScalar(val)))
      (equal(pType "string") oagitSymExpString(val))
      (t oagitSymExpScalar(val))
    )
  )
)

procedure(oagitSymExpPropPresentP(prop)
  let((pType)
    pType = oagitSymExpString(prop~>valueType)
    if(equal(pType "boolean")
        then t
        else prop~>value != nil)
  )
)

procedure(oagitSymExpScalarListP(val)
  let((ok cur elem)
    if(val == nil
        then t
        else
          ok = listp(val)
          cur = val
          while(ok && dtpr(cur)
            elem = car(cur)
            unless(stringp(elem) || integerp(elem) ||
                   (floatp(elem) && oagitJsonFiniteFloatP(elem)) ||
                   eq(elem t) || eq(elem nil)
              ok = nil)
            cur = cdr(cur)
          )
          when(cur != nil
            ok = nil)
          ok)
  )
)

procedure(oagitSymExpScalarListValue(val)
  let((out elem encoded)
    out = nil
    foreach(elem val
      encoded = cond(
        (eq(elem t) list('jsonBool t))
        (eq(elem nil) list('jsonBool nil))
        (stringp(elem) elem)
        (integerp(elem) elem)
        (floatp(elem) float(elem))
        (t list('jsonNull))
      )
      out = cons(encoded out)
    )
    oagitSymExpArray(reverse(out))
  )
)

procedure(oagitSymExpProperties(cv)
  let((props portOrder omissions name pType item)
    props = nil
    portOrder = nil
    omissions = nil
    foreach(prop cv~>prop
      name = oagitSymExpString(prop~>name)
      pType = oagitSymExpString(prop~>valueType)
      cond(
        (equal(name "portOrder") && equal(pType "ILList")
          portOrder = nil
          foreach(item prop~>value
            portOrder = cons(oagitSymExpString(item) portOrder))
          portOrder = reverse(portOrder))
        (oagitSymExpInternalPropP(name)
          /* Ignore OA internal counters and timestamp properties. */
          nil)
        (member(pType list("string" "int" "float" "boolean"))
          when(oagitSymExpPropPresentP(prop)
            props = cons(list(name oagitSymExpPropValue(prop)) props)))
        (equal(pType "ILList") && oagitSymExpScalarListP(prop~>value)
          props = cons(list(name oagitSymExpScalarListValue(prop~>value)) props))
        (t
          omissions = cons(
            oagitSymExpOmission(
              "property" name "unsupported_property_type"
              list(list("value_type" pType)))
            omissions))
      )
    )
    list(oagitSymExpObject(reverse(props))
         oagitSymExpArray(portOrder)
         reverse(omissions))
  )
)

procedure(oagitSymExpClose(cv openedByScript)
  when(cv && openedByScript
    errset(dbClose(cv) t)
  )
)

procedure(oagitSymBuildResponse(cv lib cell view)
  let((pinFigIds pinResult pins pinOmissions
       gfxResult graphics gfxOmissions
       propResult props portOrder propOmissions
       allOmissions metadata)
    pinFigIds = makeTable("oagitSymPinFigIds" nil)
    pinResult = oagitSymExpPins(cv pinFigIds)
    pins = car(pinResult)
    pinOmissions = cadr(pinResult)

    gfxResult = oagitSymExpGraphics(cv pinFigIds)
    graphics = car(gfxResult)
    gfxOmissions = cadr(gfxResult)

    propResult = oagitSymExpProperties(cv)
    props = car(propResult)
    portOrder = cadr(propResult)
    propOmissions = caddr(propResult)

    allOmissions = append(pinOmissions append(gfxOmissions propOmissions))
    metadata = list('jsonObject
                    list("cellview_type" "schematicSymbol")
                    list("dbu_per_uu" oagitSymExpScalar(cv~>DBUPerUU)))
    list('jsonObject
         list("ok" list('jsonBool t))
         list("error" list('jsonNull))
         list("cellview"
              list('jsonObject
                   list("library" oagitSymExpString(lib))
                   list("cell" oagitSymExpString(cell))
                   list("view" oagitSymExpString(view))))
         list("modified_but_not_saved" list('jsonBool nil))
         list("metadata" metadata)
         list("pins" pins)
         list("port_order" portOrder)
         list("graphics" graphics)
         list("properties" props)
         list("omissions" oagitSymExpArray(allOmissions)))
  )
)

procedure(oagitSymRunExport(req)
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
        oagitFail(outPath "open_failed" "unable to open requested symbol CellView"))
      (cv~>modifiedButNotSaved
        oagitSymExpClose(cv openedByScript)
        oagitFail(outPath "modified_but_not_saved"
                  "symbol CellView has unsaved modifications"))
      (t
        captureResult = errset(oagitSymBuildResponse(cv lib cell view) t)
        if(captureResult
            then
              response = car(captureResult)
              errset(oagitWriteJsonFile(outPath response) t)
              oagitSymExpClose(cv openedByScript)
              response
            else
              oagitSymExpClose(cv openedByScript)
              oagitFail(outPath "capture_failed"
                        sprintf(nil "unexpected symbol capture error: %L"
                                errset.errset))))
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
     oagitRequest->action == "capture_symbol"
  oagitSymRunExport(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
