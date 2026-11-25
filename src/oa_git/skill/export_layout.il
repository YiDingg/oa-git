/* Official-API read-only layout capture for the oa-git bridge. */

procedure(oagitLayoutExportString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitLayoutExportMaybeString(value)
  let((text)
    if(value
        then
          text = oagitLayoutExportString(value)
          if(strlen(text) > 0 text list('jsonNull))
        else list('jsonNull))
  )
)

procedure(oagitLayoutExportBoolVal(value)
  if(equal(value t) || equal(value "TRUE") || equal(value "true")
      then t
      else nil)
)

procedure(oagitLayoutExportBool(value)
  list('jsonBool oagitLayoutExportBoolVal(value))
)

procedure(oagitLayoutExportFiniteNumberP(value)
  integerp(value) || (floatp(value) && oagitJsonFiniteFloatP(value))
)

procedure(oagitLayoutExportScalar(value)
  cond(
    (!value list('jsonNull))
    (eq(value t) list('jsonBool t))
    (stringp(value) value)
    (integerp(value) value)
    (floatp(value)
      if(oagitJsonFiniteFloatP(value) value list('jsonNull)))
    (t list('jsonNull))
  )
)

procedure(oagitLayoutExportInt(value defaultVal)
  cond(
    (integerp(value) value)
    (floatp(value) && oagitJsonFiniteFloatP(value) fix(value))
    (t defaultVal)
  )
)

procedure(oagitLayoutExportFloat(value defaultVal)
  cond(
    (floatp(value) && oagitJsonFiniteFloatP(value) value)
    (integerp(value) float(value))
    (t defaultVal)
  )
)

procedure(oagitLayoutExportObject(pairs)
  cons('jsonObject pairs)
)

procedure(oagitLayoutExportArray(items)
  cons('jsonArray items)
)

procedure(oagitLayoutExportPointObject(point)
  let((xResult yResult x y)
    xResult = errset(point~>x t)
    yResult = errset(point~>y t)
    if(xResult && yResult &&
       oagitLayoutExportFiniteNumberP(car(xResult)) &&
       oagitLayoutExportFiniteNumberP(car(yResult))
        then
          x = car(xResult)
          y = car(yResult)
          list('jsonArray x y)
        else list('jsonArray))
  )
)

procedure(oagitLayoutExportPoint(point)
  cond(
    (point && listp(point) &&
     oagitLayoutExportFiniteNumberP(car(point)) &&
     oagitLayoutExportFiniteNumberP(cadr(point))
      list('jsonArray car(point) cadr(point)))
    (point oagitLayoutExportPointObject(point))
    (t list('jsonArray))
  )
)

procedure(oagitLayoutExportPointOrDefault(point defaultPt)
  let((encoded)
    encoded = oagitLayoutExportPoint(point)
    if(length(cdr(encoded)) == 2
        then encoded
        else oagitLayoutExportPoint(defaultPt))
  )
)

procedure(oagitLayoutExportPoints(points)
  let((out index count)
    out = nil
    if(points && numberp(car(points))
        then
          index = 0
          count = length(points)
          while(index + 1 < count
            out = cons(oagitLayoutExportPoint(
                         list(nth(index points) nth(index + 1 points))) out)
            index = index + 2
          )
        else
          foreach(point points
            out = cons(oagitLayoutExportPoint(point) out)))
    oagitLayoutExportArray(reverse(out))
  )
)

procedure(oagitLayoutExportBBox(bbox)
  if(bbox && listp(bbox) && car(bbox) && cadr(bbox)
      then list('jsonArray
                oagitLayoutExportPoint(car(bbox))
                oagitLayoutExportPoint(cadr(bbox)))
      else list('jsonArray))
)

procedure(oagitLayoutExportBBoxPoints(bbox)
  let((ll ur x1 y1 x2 y2)
    if(bbox && listp(bbox) && car(bbox) && cadr(bbox)
        then
          ll = car(bbox)
          ur = cadr(bbox)
          x1 = car(ll)
          y1 = cadr(ll)
          x2 = car(ur)
          y2 = cadr(ur)
          list(list(x1 y1) list(x2 y1) list(x2 y2) list(x1 y2))
        else nil)
  )
)

procedure(oagitLayoutExportLayerPurpose(fig defaultPurpose)
  let((lpp layer purpose)
    lpp = fig~>lpp
    layer = if(listp(lpp) && car(lpp)
                oagitLayoutExportString(car(lpp))
                oagitLayoutExportString(fig~>layerName))
    purpose = if(listp(lpp) && cadr(lpp)
                  oagitLayoutExportString(cadr(lpp))
                  if(fig~>purpose
                      oagitLayoutExportString(fig~>purpose)
                      defaultPurpose))
    list(layer purpose)
  )
)

procedure(oagitLayoutExportStrippedMetaPropP(name)
  member(name list(
    "lxComponentType"
    "description"
    "pdkVersion"
    "macroArgumentStyle"
    "lxInternal"
    "pul_schematic_instance_properties"
    "pul_layout_instance_properties"
    "lastSchematicExtraction"
    "schLastCheckTime"
    "schGeometryLastUpdated"
    "schGeometryVersion"
    "schVersion"
    "connUpdateTime"
    "connectivityLastUpdated"
    "instancesLastUpdated"
    "instancesLastChanged"
    "interfaceLastChanged"
    "lastSavedExtractCounter"
    "viewSubType"
    "pin#"
    "inst#"
    "instance#"
    "net#"
  ))
)

procedure(oagitLayoutExportDisplayPropP(name)
  member(name list(
    "accessEdgesOn"
    "arrayDisplay"
    "autoZoomMode"
    "autoZoomPan"
    "autoZoomScale"
    "cursorShape"
    "dimGridLines"
    "dimmingAutoEnabled"
    "dimmingIntensity"
    "dimmingOn"
    "dimmingScope"
    "displayFigGroupName"
    "displayInstanceOrientation"
    "displayPRBoundaryAtCurrentLevelOnly"
    "displayPRHighestResolution"
    "displayPinNames"
    "displayResolution"
    "displayRowName"
    "displayRowSite"
    "displaySelectionHighlightWhenDimmed"
    "displayStretchHandles"
    "displayViaShapes"
    "dotStyleMajorGrid"
    "dotsOn"
    "drawAxesOn"
    "drawDottedGridOn"
    "drawGridOn"
    "drawInstancePins"
    "drawSurroundingOn"
    "drawTraversedInstBbox"
    "dynamicHilightOn"
    "filterSize"
    "filterSizeDrawingStyle"
    "ghostOfDraggedObjects"
    "gridMultiple"
    "gridSpacing"
    "iconsOn"
    "instLabel"
    "instanceDrawingMode"
    "leDispNetExpr"
    "lppVisibilityMode"
    "mergeEnable"
    "mergeScope"
    "netsOn"
    "openToStopLevel"
    "originMarkersOn"
    "pathCL"
    "scrollPercent"
    "segSnapMode"
    "selDisplayInstContent"
    "selectionHintOn"
    "slotEnable"
    "snapMode"
    "snapPatternDrawingStyle"
    "startLevel"
    "stopLevel"
    "textJustificationOn"
    "transparentGroup"
    "trimEnable"
    "useTrueBBox"
    "xSnapSpacing"
    "ySnapSpacing"
  ))
)

procedure(oagitLayoutExportScalarPropValidP(prop)
  let((propType value)
    propType = oagitLayoutExportString(prop~>valueType)
    value = prop~>value
    cond(
      (equal(propType "boolean") t)
      (equal(propType "string") stringp(value) || symbolp(value) || !value)
      (equal(propType "int") oagitLayoutExportFiniteNumberP(value))
      (equal(propType "float") oagitLayoutExportFiniteNumberP(value))
      (t nil)
    )
  )
)

procedure(oagitLayoutExportPropValue(prop)
  let((propType value)
    propType = oagitLayoutExportString(prop~>valueType)
    value = prop~>value
    cond(
      (equal(propType "boolean") oagitLayoutExportBool(value))
      (equal(propType "int")
        if(integerp(value) value fix(value)))
      (equal(propType "float") float(value))
      (equal(propType "string") oagitLayoutExportString(value))
      (t oagitLayoutExportScalar(value))
    )
  )
)

procedure(oagitLayoutExportPropsObject(props)
  let((out propName propType)
    out = nil
    foreach(prop props
      propName = oagitLayoutExportString(prop~>name)
      propType = oagitLayoutExportString(prop~>valueType)
      when(strlen(propName) > 0 &&
           !equal(propType "hierProp") &&
           !oagitLayoutExportStrippedMetaPropP(propName) &&
           member(propType list("string" "int" "float" "boolean")) &&
           oagitLayoutExportScalarPropValidP(prop)
        out = cons(list(propName oagitLayoutExportPropValue(prop)) out)
      )
    )
    oagitLayoutExportObject(reverse(out))
  )
)

procedure(oagitLayoutExportCellProperties(props)
  let((out propName propType)
    out = nil
    foreach(prop props
      propName = oagitLayoutExportString(prop~>name)
      propType = oagitLayoutExportString(prop~>valueType)
      when(strlen(propName) > 0 &&
           !equal(propType "hierProp") &&
           !oagitLayoutExportStrippedMetaPropP(propName) &&
           !oagitLayoutExportDisplayPropP(propName) &&
           member(propType list("string" "int" "float" "boolean")) &&
           oagitLayoutExportScalarPropValidP(prop)
        out = cons(list(propName oagitLayoutExportPropValue(prop)) out)
      )
    )
    oagitLayoutExportObject(reverse(out))
  )
)

procedure(oagitLayoutExportAccessDirs(rawAccessDir)
  let((out item text)
    out = nil
    cond(
      (listp(rawAccessDir)
        foreach(item rawAccessDir
          text = oagitLayoutExportString(item)
          when(strlen(text) > 0
            out = cons(text out))
        ))
      (rawAccessDir
        text = oagitLayoutExportString(rawAccessDir)
        when(strlen(text) > 0
          out = cons(text out)))
    )
    oagitLayoutExportArray(reverse(out))
  )
)

procedure(oagitLayoutExportOmission(kind identifier reason detailsPairs)
  list('jsonObject
       list("kind" oagitLayoutExportString(kind))
       list("identifier" oagitLayoutExportString(identifier))
       list("reason" oagitLayoutExportString(reason))
       list("details" oagitLayoutExportObject(detailsPairs)))
)

procedure(oagitLayoutExportPathSegCustomStyle(fig beginStyle endStyle)
  let((res styleInfo extSpec customTuple out elem)
    out = nil
    when(equal(beginStyle "custom") || equal(endStyle "custom")
      res = errset(dbGetPathSegStyle(fig) t)
      styleInfo = if(res car(res) nil)
      extSpec = if(listp(styleInfo) && length(styleInfo) >= 4 cadddr(styleInfo) nil)
      customTuple = if(listp(extSpec) && length(extSpec) >= 3 caddr(extSpec) nil)
      when(listp(customTuple) && length(customTuple) == 6
        foreach(elem customTuple
          when(oagitLayoutExportFiniteNumberP(elem)
            out = cons(float(elem) out))
        )
        when(length(out) != 6 out = nil)
      )
    )
    oagitLayoutExportArray(reverse(out))
  )
)

procedure(oagitLayoutExportPins(cv pinFigIds)
  let((out omissions termName netName direction pinName figs accessDirs
       objType lpp layer purpose props pinObj beginStyle endStyle pathStyle)
    out = nil
    omissions = nil
    foreach(term cv~>terminals
      termName = oagitLayoutExportString(term~>name)
      netName = if(term~>net && term~>net~>name
                    oagitLayoutExportString(term~>net~>name)
                    termName)
      direction = if(term~>direction
                      oagitLayoutExportString(term~>direction)
                      "inputOutput")
      foreach(pin term~>pins
        pinName = if(pin~>name oagitLayoutExportString(pin~>name) "")
        figs = pin~>figs
        unless(figs
          figs = if(pin~>fig list(pin~>fig) nil))
        accessDirs = oagitLayoutExportAccessDirs(pin~>accessDir)
        foreach(fig figs
          when(fig
            pinFigIds[fig] = t
            objType = oagitLayoutExportString(fig~>objType)
            lpp = oagitLayoutExportLayerPurpose(fig "pin")
            layer = car(lpp)
            purpose = cadr(lpp)
            props = oagitLayoutExportPropsObject(fig~>prop)
            cond(
              (equal(objType "rect") || equal(objType "ellipse")
                pinObj = list('jsonObject
                              list("type" "pin")
                              list("name" termName)
                              list("pin_name" pinName)
                              list("net" netName)
                              list("direction" direction)
                              list("layer" layer)
                              list("purpose" purpose)
                              list("shape_kind" objType)
                              list("bbox" oagitLayoutExportBBox(fig~>bBox))
                              list("access_dir" accessDirs)
                              list("props" props))
                out = cons(pinObj out))
              (equal(objType "polygon")
                pinObj = list('jsonObject
                              list("type" "pin")
                              list("name" termName)
                              list("pin_name" pinName)
                              list("net" netName)
                              list("direction" direction)
                              list("layer" layer)
                              list("purpose" purpose)
                              list("shape_kind" "polygon")
                              list("points" oagitLayoutExportPoints(fig~>points))
                              list("access_dir" accessDirs)
                              list("props" props))
                out = cons(pinObj out))
              (equal(objType "pathSeg")
                beginStyle = if(fig~>beginStyle
                                 oagitLayoutExportString(fig~>beginStyle)
                                 "truncate")
                endStyle = if(fig~>endStyle
                               oagitLayoutExportString(fig~>endStyle)
                               "truncate")
                pinObj = list('jsonObject
                              list("type" "pin")
                              list("name" termName)
                              list("pin_name" pinName)
                              list("net" netName)
                              list("direction" direction)
                              list("layer" layer)
                              list("purpose" purpose)
                              list("shape_kind" "path_seg")
                              list("begin_pt" oagitLayoutExportPointOrDefault(fig~>beginPt list(0.0 0.0)))
                              list("end_pt" oagitLayoutExportPointOrDefault(fig~>endPt list(0.0 0.0)))
                              list("width" oagitLayoutExportFloat(fig~>width 0.0))
                              list("begin_style" beginStyle)
                              list("end_style" endStyle)
                              list("begin_ext" oagitLayoutExportFloat(fig~>beginExt 0.0))
                              list("end_ext" oagitLayoutExportFloat(fig~>endExt 0.0))
                              list("custom_style" oagitLayoutExportPathSegCustomStyle(fig beginStyle endStyle))
                              list("access_dir" accessDirs)
                              list("props" props))
                out = cons(pinObj out))
              (equal(objType "path")
                pathStyle = if(fig~>pathStyle
                                oagitLayoutExportString(fig~>pathStyle)
                                "truncateExtend")
                pinObj = list('jsonObject
                              list("type" "pin")
                              list("name" termName)
                              list("pin_name" pinName)
                              list("net" netName)
                              list("direction" direction)
                              list("layer" layer)
                              list("purpose" purpose)
                              list("shape_kind" "path")
                              list("points" oagitLayoutExportPoints(fig~>points))
                              list("width" oagitLayoutExportFloat(fig~>width 0.0))
                              list("path_style" pathStyle)
                              list("begin_ext" oagitLayoutExportFloat(fig~>beginExt 0.0))
                              list("end_ext" oagitLayoutExportFloat(fig~>endExt 0.0))
                              list("access_dir" accessDirs)
                              list("props" props))
                out = cons(pinObj out))
              (t
                omissions = cons(
                  oagitLayoutExportOmission(
                    "pin_shape"
                    termName
                    "unsupported_pin_shape"
                    list(list("obj_type" objType)
                         list("layer" oagitLayoutExportMaybeString(layer))
                         list("purpose" oagitLayoutExportMaybeString(purpose))
                         list("pin_name" pinName)))
                  omissions))
            )
          )
        )
      )
    )
    list(reverse(out) reverse(omissions))
  )
)

procedure(oagitLayoutExportNets(cv)
  let((out numBits sigType)
    out = nil
    foreach(net cv~>nets
      numBits = oagitLayoutExportInt(net~>numBits 1)
      when(numBits < 1 numBits = 1)
      sigType = if(net~>sigType && strlen(oagitLayoutExportString(net~>sigType)) > 0
                    oagitLayoutExportString(net~>sigType)
                    "signal")
      out = cons(
        list('jsonObject
             list("type" "net")
             list("name" oagitLayoutExportString(net~>name))
             list("is_global" oagitLayoutExportBool(net~>isGlobal))
             list("num_bits" numBits)
             list("sig_type" sigType)
             list("props" oagitLayoutExportPropsObject(net~>prop)))
        out)
    )
    reverse(out)
  )
)

procedure(oagitLayoutExportFormalParamTable(master)
  let((formalTable defaultTable superMaster isPCell paramRes fpNameRes fpName)
    formalTable = makeTable("oagitLayoutFormalParams" nil)
    defaultTable = makeTable("oagitLayoutFormalDefaults" nil)
    superMaster = nil
    isPCell = nil
    when(master
      isPCell = if(master~>isParamCell || master~>superMaster t nil)
      superMaster = master~>superMaster
      unless(superMaster
        when(master~>isParamCell superMaster = master))
      when(isPCell && superMaster
        paramRes = errset(superMaster~>parameters~>value t)
        when(paramRes && car(paramRes) && listp(car(paramRes))
          foreach(fp car(paramRes)
            fpNameRes = errset(fp~>name t)
            when(fpNameRes && car(fpNameRes)
              fpName = oagitLayoutExportString(car(fpNameRes))
              formalTable[fpName] = t
              when(oagitLayoutExportScalarPropValidP(fp)
                defaultTable[fpName] = list(
                  oagitLayoutExportString(fp~>valueType)
                  oagitLayoutExportPropValue(fp)))
            )
          )
        )
      )
    )
    list(isPCell formalTable defaultTable)
  )
)

procedure(oagitLayoutExportSplitProps(propLists isPCell formalTable defaultTable)
  let((seen pcellParams pcellParamTypes props propTypes pName pType val defEntry)
    seen = makeTable("oagitLayoutSeenProps" nil)
    pcellParams = nil
    pcellParamTypes = nil
    props = nil
    propTypes = nil
    foreach(propList propLists
      foreach(prop propList
        pName = oagitLayoutExportString(prop~>name)
        pType = oagitLayoutExportString(prop~>valueType)
        when(strlen(pName) > 0 &&
             !seen[pName] &&
             !equal(pType "hierProp") &&
             !oagitLayoutExportStrippedMetaPropP(pName) &&
             member(pType list("string" "int" "float" "boolean")) &&
             oagitLayoutExportScalarPropValidP(prop)
          seen[pName] = t
          val = oagitLayoutExportPropValue(prop)
          if(isPCell && formalTable[pName]
              then
                defEntry = if(defaultTable defaultTable[pName] nil)
                /* Skip formal PCell params equal to superMaster default because
                   dbCreateParamInst normalizes defaults out of inst~>prop. */
                unless(defEntry && equal(car(defEntry) pType) && equal(cadr(defEntry) val)
                  pcellParams = cons(list(pName val) pcellParams)
                  pcellParamTypes = cons(list(pName pType) pcellParamTypes))
              else
                props = cons(list(pName val) props)
                propTypes = cons(list(pName pType) propTypes))
        )
      )
    )
    list(reverse(pcellParams)
         reverse(pcellParamTypes)
         reverse(props)
         reverse(propTypes))
  )
)

procedure(oagitLayoutExportConnections(inst)
  let((pairs netName termName)
    pairs = nil
    foreach(it inst~>instTerms
      when(it~>net && it~>net~>name
        netName = oagitLayoutExportString(it~>net~>name)
        termName = oagitLayoutExportString(it~>name)
        when(strlen(netName) > 0 && strlen(termName) > 0
          pairs = cons(list(termName netName) pairs))
      )
    )
    oagitLayoutExportObject(reverse(pairs))
  )
)

procedure(oagitLayoutExportIsMosaicInst(inst mosaicInstIds)
  let((objType)
    objType = oagitLayoutExportString(inst~>objType)
    (mosaicInstIds && mosaicInstIds[inst]) ||
    inst~>mosaic ||
    equal(objType "mosaicInst") ||
    equal(objType "mosaic")
  )
)

procedure(oagitLayoutExportInstances(cv pinFigIds mosaicInstIds)
  let((out pcellInfo isPCell formalTable defaultTable split pcellParams
       pcellParamTypes props propTypes)
    out = nil
    foreach(inst cv~>instances
      unless(pinFigIds[inst] || oagitLayoutExportIsMosaicInst(inst mosaicInstIds)
        pcellInfo = oagitLayoutExportFormalParamTable(inst~>master)
        isPCell = car(pcellInfo)
        formalTable = cadr(pcellInfo)
        defaultTable = caddr(pcellInfo)
        split = oagitLayoutExportSplitProps(
                  list(inst~>prop) isPCell formalTable defaultTable)
        pcellParams = car(split)
        pcellParamTypes = cadr(split)
        props = caddr(split)
        propTypes = cadddr(split)
        out = cons(
          list('jsonObject
               list("type" "instance")
               list("name" oagitLayoutExportString(inst~>name))
               list("lib" oagitLayoutExportString(inst~>libName))
               list("cell" oagitLayoutExportString(inst~>cellName))
               list("view" if(inst~>viewName
                               oagitLayoutExportString(inst~>viewName)
                               "layout"))
               list("xy" oagitLayoutExportPointOrDefault(inst~>xy list(0.0 0.0)))
               list("orient" if(inst~>orient
                                 oagitLayoutExportString(inst~>orient)
                                 "R0"))
               list("is_pcell" oagitLayoutExportBool(isPCell))
               list("pcell_params" oagitLayoutExportObject(pcellParams))
               list("pcell_param_types" oagitLayoutExportObject(pcellParamTypes))
               list("connections" oagitLayoutExportConnections(inst))
               list("props" oagitLayoutExportObject(props))
               list("prop_types" oagitLayoutExportObject(propTypes)))
          out)
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutExportMosaics(cv mosaicInstIds)
  let((out mosaicsRes tplInst master mLib mCell mView mOrient rows cols uX uY
       pcellInfo isPCell formalTable defaultTable propLists split pcellParams
       pcellParamTypes props propTypes mi)
    out = nil
    mosaicsRes = errset(cv~>mosaics t)
    when(mosaicsRes && car(mosaicsRes)
      foreach(m car(mosaicsRes)
        when(mosaicInstIds
          mosaicInstIds[m] = t
          when(listp(m~>instanceList)
            foreach(mi m~>instanceList
              when(mi mosaicInstIds[mi] = t)
            )
          )
        )
        tplInst = if(listp(m~>instanceList) car(m~>instanceList) nil)
        master = m~>master
        unless(master when(tplInst master = tplInst~>master))
        mLib = m~>libName
        unless(mLib when(tplInst mLib = tplInst~>libName))
        unless(mLib when(master mLib = master~>libName))
        mCell = m~>cellName
        unless(mCell when(tplInst mCell = tplInst~>cellName))
        unless(mCell when(master mCell = master~>cellName))
        mView = m~>viewName
        unless(mView when(tplInst mView = tplInst~>viewName))
        unless(mView when(master mView = master~>viewName))
        unless(mView mView = "layout")
        mOrient = m~>orient
        unless(mOrient
          when(listp(m~>tileArray) && car(m~>tileArray)
            mOrient = car(m~>tileArray)))
        unless(mOrient when(tplInst mOrient = tplInst~>orient))
        unless(mOrient mOrient = "R0")
        rows = oagitLayoutExportInt(m~>rows 1)
        when(rows < 1 rows = 1)
        cols = oagitLayoutExportInt(m~>columns 1)
        when(cols < 1 cols = 1)
        uX = oagitLayoutExportFloat(m~>uX 0.0)
        uY = oagitLayoutExportFloat(m~>uY 0.0)
        pcellInfo = oagitLayoutExportFormalParamTable(master)
        isPCell = car(pcellInfo)
        formalTable = cadr(pcellInfo)
        defaultTable = caddr(pcellInfo)
        propLists = if(tplInst list(m~>prop tplInst~>prop) list(m~>prop))
        split = oagitLayoutExportSplitProps(
                  propLists isPCell formalTable defaultTable)
        pcellParams = car(split)
        pcellParamTypes = cadr(split)
        props = caddr(split)
        propTypes = cadddr(split)
        out = cons(
          list('jsonObject
               list("type" "mosaic")
               list("name" oagitLayoutExportString(m~>name))
               list("lib" oagitLayoutExportString(mLib))
               list("cell" oagitLayoutExportString(mCell))
               list("view" oagitLayoutExportString(mView))
               list("xy" oagitLayoutExportPointOrDefault(m~>xy list(0.0 0.0)))
               list("orient" oagitLayoutExportString(mOrient))
               list("rows" rows)
               list("columns" cols)
               list("u_x" uX)
               list("u_y" uY)
               list("is_pcell" oagitLayoutExportBool(isPCell))
               list("pcell_params" oagitLayoutExportObject(pcellParams))
               list("pcell_param_types" oagitLayoutExportObject(pcellParamTypes))
               list("props" oagitLayoutExportObject(props))
               list("prop_types" oagitLayoutExportObject(propTypes)))
          out)
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutExportViaParamValue(val)
  let((nums valid elem)
    cond(
      (integerp(val) list(t val))
      (floatp(val)
        if(oagitJsonFiniteFloatP(val) list(t val) list(nil nil)))
      (stringp(val) list(t val))
      (eq(val t) list(t list('jsonBool t)))
      (!val list(t list('jsonBool nil)))
      (symbolp(val) list(t oagitLayoutExportString(val)))
      (listp(val)
        nums = nil
        valid = t
        foreach(elem val
          if(oagitLayoutExportFiniteNumberP(elem)
              then nums = cons(elem nums)
              else valid = nil)
        )
        if(valid && nums
            then list(t oagitLayoutExportArray(reverse(nums)))
            else list(nil nil)))
      (t list(nil nil))
    )
  )
)

procedure(oagitLayoutExportViaValEqualP(a b)
  let((ok remA remB x y)
    cond(
      (numberp(a) && numberp(b)
        abs(float(a) - float(b)) <= 1.0e-9 * max(abs(float(a)) abs(float(b)) 1.0))
      (listp(a) && listp(b) && length(a) == length(b)
        ok = t
        remA = a
        remB = b
        while(ok && remA
          x = car(remA)
          y = car(remB)
          unless(oagitLayoutExportViaValEqualP(x y)
            ok = nil)
          remA = cdr(remA)
          remB = cdr(remB)
        )
        ok)
      (t equal(a b))
    )
  )
)

procedure(oagitLayoutExportStdViaDefault(viaDef key)
  let((pRes p cutSpec)
    pRes = if(viaDef errset(viaDef~>params t) nil)
    p = if(pRes car(pRes) nil)
    when(listp(p) && length(p) >= 9
      /* Handle both flat IC26.1 stdViaDef~>params:
           (cutLayer cutW cutH cutRows cutCols cutSpacing l1Enc l2Enc l1Off l2Off origOff imp1 imp2)
         and nested 3rd-element form:
           (cutLayer cutW cutH (cutRows cutCols cutSpacing) l1Enc l2Enc ...) */
      cutSpec = nth(3 p)
      if(listp(cutSpec)
          then
            cond(
              (equal(key "cutWidth") nth(1 p))
              (equal(key "cutHeight") nth(2 p))
              (equal(key "cutRows") car(cutSpec))
              (equal(key "cutColumns") if(cdr(cutSpec) cadr(cutSpec) 1))
              (equal(key "cutSpacing") if(cddr(cutSpec) caddr(cutSpec) nil))
              (equal(key "layer1Enc") nth(4 p))
              (equal(key "layer2Enc") nth(5 p))
              (equal(key "layer1Offset") nth(6 p))
              (equal(key "layer2Offset") nth(7 p))
              (equal(key "originOffset") nth(8 p))
              (equal(key "imp1Enc") if(length(p) >= 10 && nth(9 p) nth(9 p) list(0.0 0.0)))
              (equal(key "imp2Enc") if(length(p) >= 11 && nth(10 p) nth(10 p) list(0.0 0.0)))
              (t nil))
          else
            cond(
              (equal(key "cutWidth") nth(1 p))
              (equal(key "cutHeight") nth(2 p))
              (equal(key "cutRows") nth(3 p))
              (equal(key "cutColumns") nth(4 p))
              (equal(key "cutSpacing") nth(5 p))
              (equal(key "layer1Enc") nth(6 p))
              (equal(key "layer2Enc") nth(7 p))
              (equal(key "layer1Offset") nth(8 p))
              (equal(key "layer2Offset") if(length(p) >= 10 nth(9 p) list(0.0 0.0)))
              (equal(key "originOffset") if(length(p) >= 11 nth(10 p) list(0.0 0.0)))
              (equal(key "imp1Enc") if(length(p) >= 12 && nth(11 p) nth(11 p) list(0.0 0.0)))
              (equal(key "imp2Enc") if(length(p) >= 13 && nth(12 p) nth(12 p) list(0.0 0.0)))
              (t nil)))
    )
  )
)

procedure(oagitLayoutExportViaParams(overrideParams viaDef viaKind)
  let((out key rawVal defVal encoded)
    out = nil
    when(listp(overrideParams)
      foreach(entry overrideParams
        when(listp(entry) && car(entry)
          key = oagitLayoutExportString(car(entry))
          rawVal = cadr(entry)
          when(strlen(key) > 0
            defVal = if(equal(viaKind "stdVia")
                         oagitLayoutExportStdViaDefault(viaDef key)
                         nil)
            unless(defVal != nil && oagitLayoutExportViaValEqualP(rawVal defVal)
              encoded = oagitLayoutExportViaParamValue(rawVal)
              when(car(encoded)
                out = cons(list(key cadr(encoded)) out)))
          )
        )
      )
    )
    oagitLayoutExportObject(reverse(out))
  )
)

procedure(oagitLayoutExportVias(cv)
  let((out omissions viasRes viaKind header viaDef viaDefName netName viaName
       overrideParams)
    out = nil
    omissions = nil
    viasRes = errset(cv~>vias t)
    when(viasRes && car(viasRes)
      foreach(via car(viasRes)
        viaKind = oagitLayoutExportString(via~>objType)
        if(equal(viaKind "stdVia") || equal(viaKind "customVia")
            then
              header = via~>viaHeader
              viaDef = if(header header~>viaDef nil)
              viaDefName = if(header && header~>viaDefName
                               oagitLayoutExportString(header~>viaDefName)
                               if(viaDef && viaDef~>name
                                   oagitLayoutExportString(viaDef~>name)
                                   ""))
              netName = if(via~>net && via~>net~>name via~>net~>name nil)
              viaName = via~>name
              overrideParams = if(header header~>overrideParams nil)
              out = cons(
                list('jsonObject
                     list("type" "via")
                     list("via_def" viaDefName)
                     list("via_kind" viaKind)
                     list("origin" oagitLayoutExportPointOrDefault(via~>origin list(0.0 0.0)))
                     list("orient" if(via~>orient
                                       oagitLayoutExportString(via~>orient)
                                       "R0"))
                     list("net" oagitLayoutExportMaybeString(netName))
                     list("name" oagitLayoutExportMaybeString(viaName))
                     list("params" oagitLayoutExportViaParams(overrideParams viaDef viaKind))
                     list("props" oagitLayoutExportPropsObject(via~>prop)))
                out)
            else
              omissions = cons(
                oagitLayoutExportOmission(
                  "via"
                  viaKind
                  "unsupported_via_type"
                  list(list("obj_type" viaKind)))
                omissions))
      )
    )
    list(reverse(out) reverse(omissions))
  )
)

procedure(oagitLayoutExportIsAttachedTextDisplay(shape)
  equal(oagitLayoutExportString(shape~>objType) "textDisplay") &&
  (shape~>parent || shape~>associate || shape~>owner)
)

procedure(oagitLayoutExportShapes(cv pinFigIds)
  let((out omissions objType lpp layer purpose netName maybeNet props
       pathStyle beginStyle endStyle labelType)
    out = nil
    omissions = nil
    foreach(shape cv~>shapes
      unless(pinFigIds[shape]
        objType = oagitLayoutExportString(shape~>objType)
        cond(
          (oagitLayoutExportIsAttachedTextDisplay(shape)
            /* Attached display text is regenerated by Virtuoso, not mask geometry. */
            nil)
          (t
            lpp = oagitLayoutExportLayerPurpose(shape "drawing")
            layer = car(lpp)
            purpose = cadr(lpp)
            netName = if(shape~>net && shape~>net~>name shape~>net~>name nil)
            maybeNet = oagitLayoutExportMaybeString(netName)
            props = oagitLayoutExportPropsObject(shape~>prop)
            cond(
              (equal(objType "rect")
                out = cons(
                  list('jsonObject
                       list("type" "rect")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("bbox" oagitLayoutExportBBox(shape~>bBox))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "polygon")
                out = cons(
                  list('jsonObject
                       list("type" "polygon")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("points" oagitLayoutExportPoints(shape~>points))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "path")
                pathStyle = if(shape~>pathStyle
                                oagitLayoutExportString(shape~>pathStyle)
                                "truncateExtend")
                out = cons(
                  list('jsonObject
                       list("type" "path")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("points" oagitLayoutExportPoints(shape~>points))
                       list("width" oagitLayoutExportFloat(shape~>width 0.0))
                       list("path_style" pathStyle)
                       list("begin_ext" oagitLayoutExportFloat(shape~>beginExt 0.0))
                       list("end_ext" oagitLayoutExportFloat(shape~>endExt 0.0))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "pathSeg")
                beginStyle = if(shape~>beginStyle
                                 oagitLayoutExportString(shape~>beginStyle)
                                 "truncate")
                endStyle = if(shape~>endStyle
                               oagitLayoutExportString(shape~>endStyle)
                               "truncate")
                out = cons(
                  list('jsonObject
                       list("type" "path_seg")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("begin_pt" oagitLayoutExportPoint(shape~>beginPt))
                       list("end_pt" oagitLayoutExportPoint(shape~>endPt))
                       list("width" oagitLayoutExportFloat(shape~>width 0.0))
                       list("begin_style" beginStyle)
                       list("end_style" endStyle)
                       list("begin_ext" oagitLayoutExportFloat(shape~>beginExt 0.0))
                       list("end_ext" oagitLayoutExportFloat(shape~>endExt 0.0))
                       list("custom_style" oagitLayoutExportPathSegCustomStyle(shape beginStyle endStyle))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "ellipse")
                out = cons(
                  list('jsonObject
                       list("type" "ellipse")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("bbox" oagitLayoutExportBBox(shape~>bBox))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "line")
                out = cons(
                  list('jsonObject
                       list("type" "line")
                       list("layer" layer)
                       list("purpose" purpose)
                       list("points" oagitLayoutExportPoints(shape~>points))
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (equal(objType "label")
                labelType = if(shape~>labelType
                                oagitLayoutExportString(shape~>labelType)
                                "normalLabel")
                out = cons(
                  list('jsonObject
                       list("type" "label")
                       list("text" oagitLayoutExportString(shape~>theLabel))
                       list("xy" oagitLayoutExportPointOrDefault(shape~>xy list(0.0 0.0)))
                       list("layer" layer)
                       list("purpose" purpose)
                       list("justify" if(shape~>justify
                                          oagitLayoutExportString(shape~>justify)
                                          "centerCenter"))
                       list("orient" if(shape~>orient
                                         oagitLayoutExportString(shape~>orient)
                                         "R0"))
                       list("font" if(shape~>font
                                       oagitLayoutExportString(shape~>font)
                                       "stick"))
                       list("height" oagitLayoutExportFloat(shape~>height 0.1))
                       list("label_type" labelType)
                       list("net" maybeNet)
                       list("props" props))
                  out))
              (t
                omissions = cons(
                  oagitLayoutExportOmission(
                    "shape"
                    objType
                    "unsupported_shape"
                    list(list("obj_type" objType)
                         list("layer" oagitLayoutExportMaybeString(layer))
                         list("purpose" oagitLayoutExportMaybeString(purpose))))
                  omissions))
            ))
        )
      )
    )
    list(reverse(out) reverse(omissions))
  )
)

procedure(oagitLayoutExportBlockages(cv)
  let((out blkRes blkType blkLayer)
    out = nil
    blkRes = errset(cv~>blockages t)
    when(blkRes && car(blkRes)
      foreach(blk car(blkRes)
        blkType = if(blk~>type oagitLayoutExportString(blk~>type) "routing")
        blkLayer = blk~>layer
        out = cons(
          list('jsonObject
               list("type" "blockage")
               list("blockage_type" blkType)
               list("layer" oagitLayoutExportMaybeString(blkLayer))
               list("points" oagitLayoutExportPoints(blk~>points))
               list("props" oagitLayoutExportPropsObject(blk~>prop)))
          out)
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutExportBoundaryRecord(bnd boundaryKind)
  let((pts)
    pts = bnd~>points
    unless(pts
      pts = oagitLayoutExportBBoxPoints(bnd~>bBox))
    list('jsonObject
         list("type" "boundary")
         list("boundary_kind" boundaryKind)
         list("points" oagitLayoutExportPoints(pts))
         list("name" oagitLayoutExportMaybeString(bnd~>name))
         list("props" oagitLayoutExportPropsObject(bnd~>prop)))
  )
)

procedure(oagitLayoutExportBoundaries(cv)
  let((out seen prRes areaRes areaSingleRes snapRes snapSingleRes clusterRes bnd)
    out = nil
    seen = makeTable("oagitLayoutSeenBoundaries" nil)
    prRes = errset(cv~>prBoundary t)
    when(prRes && car(prRes) && !seen[car(prRes)]
      seen[car(prRes)] = t
      out = cons(oagitLayoutExportBoundaryRecord(car(prRes) "prBoundary") out))
    areaSingleRes = errset(cv~>areaBoundary t)
    when(areaSingleRes && car(areaSingleRes) && !listp(car(areaSingleRes)) && !seen[car(areaSingleRes)]
      seen[car(areaSingleRes)] = t
      out = cons(oagitLayoutExportBoundaryRecord(car(areaSingleRes) "areaBoundary") out))
    areaRes = errset(cv~>areaBoundaries t)
    when(areaRes && car(areaRes) && listp(car(areaRes))
      foreach(bnd car(areaRes)
        when(bnd && !seen[bnd]
          seen[bnd] = t
          out = cons(oagitLayoutExportBoundaryRecord(bnd "areaBoundary") out))))
    snapSingleRes = errset(cv~>snapBoundary t)
    when(snapSingleRes && car(snapSingleRes) && !listp(car(snapSingleRes)) && !seen[car(snapSingleRes)]
      seen[car(snapSingleRes)] = t
      out = cons(oagitLayoutExportBoundaryRecord(car(snapSingleRes) "snapBoundary") out))
    snapRes = errset(cv~>snapBoundaries t)
    when(snapRes && car(snapRes) && listp(car(snapRes))
      foreach(bnd car(snapRes)
        when(bnd && !seen[bnd]
          seen[bnd] = t
          out = cons(oagitLayoutExportBoundaryRecord(bnd "snapBoundary") out))))
    clusterRes = errset(cv~>clusterBoundaries t)
    when(clusterRes && car(clusterRes) && listp(car(clusterRes))
      foreach(bnd car(clusterRes)
        when(bnd && !seen[bnd]
          seen[bnd] = t
          out = cons(oagitLayoutExportBoundaryRecord(bnd "clusterBoundary") out))))
    reverse(out)
  )
)

procedure(oagitLayoutExportFigGroups(cv)
  let((out fgRes groupType)
    out = nil
    fgRes = errset(cv~>figGroups t)
    when(fgRes && car(fgRes)
      foreach(fg car(fgRes)
        groupType = if(fg~>type && strlen(oagitLayoutExportString(fg~>type)) > 0
                        oagitLayoutExportString(fg~>type)
                        "default")
        out = cons(
          list('jsonObject
               list("type" "fig_group")
               list("name" oagitLayoutExportString(fg~>name))
               list("origin" oagitLayoutExportPointOrDefault(fg~>origin list(0.0 0.0)))
               list("orient" if(fg~>orient
                                 oagitLayoutExportString(fg~>orient)
                                 "R0"))
               list("group_type" groupType)
               list("props" oagitLayoutExportPropsObject(fg~>prop)))
          out)
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutExportTechLib(cv)
  let((techRes techLib tfRes)
    techLib = nil
    techRes = errset(techGetTechLibName(cv) t)
    when(techRes && car(techRes)
      techLib = car(techRes))
    unless(techLib
      tfRes = errset(techGetTechFile(cv) t)
      when(tfRes && car(tfRes)
        techLib = car(tfRes)~>libName))
    techLib
  )
)

procedure(oagitLayoutExportClose(cv openedByScript)
  when(cv && openedByScript
    errset(dbClose(cv) t)
  )
)

procedure(oagitLayoutExportBuildResponse(cv lib cell view)
  let((pinFigIds mosaicInstIds pinRes pins pinOmissions nets instances mosaics
       viaRes vias viaOmissions shapeRes shapes shapeOmissions
       blockages boundaries figGroups allObjects allOmissions
       cellProps techLib metadata)
    pinFigIds = makeTable("oagitLayoutPinFigIds" nil)
    mosaicInstIds = makeTable("oagitLayoutMosaicInstIds" nil)
    pinRes = oagitLayoutExportPins(cv pinFigIds)
    pins = car(pinRes)
    pinOmissions = cadr(pinRes)
    nets = oagitLayoutExportNets(cv)
    mosaics = oagitLayoutExportMosaics(cv mosaicInstIds)
    instances = oagitLayoutExportInstances(cv pinFigIds mosaicInstIds)
    viaRes = oagitLayoutExportVias(cv)
    vias = car(viaRes)
    viaOmissions = cadr(viaRes)
    shapeRes = oagitLayoutExportShapes(cv pinFigIds)
    shapes = car(shapeRes)
    shapeOmissions = cadr(shapeRes)
    blockages = oagitLayoutExportBlockages(cv)
    boundaries = oagitLayoutExportBoundaries(cv)
    figGroups = oagitLayoutExportFigGroups(cv)
    cellProps = oagitLayoutExportCellProperties(cv~>prop)
    allObjects = append(nets
                  append(pins
                  append(instances
                  append(mosaics
                  append(vias
                  append(shapes
                  append(blockages
                  append(boundaries figGroups))))))))
    allOmissions = append(pinOmissions append(viaOmissions shapeOmissions))
    techLib = oagitLayoutExportTechLib(cv)
    metadata = list('jsonObject
                    list("cellview_type"
                         if(cv~>cellViewType
                             oagitLayoutExportString(cv~>cellViewType)
                             "maskLayout"))
                    list("dbu_per_uu" oagitLayoutExportScalar(cv~>DBUPerUU))
                    list("tech_lib" oagitLayoutExportMaybeString(techLib)))
    list('jsonObject
         list("ok" list('jsonBool t))
         list("error" list('jsonNull))
         list("error_code" list('jsonNull))
         list("cellview"
              list('jsonObject
                   list("library" oagitLayoutExportString(lib))
                   list("cell" oagitLayoutExportString(cell))
                   list("view" oagitLayoutExportString(view))))
         list("modified_but_not_saved" list('jsonBool nil))
         list("metadata" metadata)
         list("objects" oagitLayoutExportArray(allObjects))
         list("properties" cellProps)
         list("omissions" oagitLayoutExportArray(allOmissions)))
  )
)

procedure(oagitLayoutExportWriteStreamValue(port value)
  /* Stream JSON directly to port in O(N) with 12-sig-fig float precision
     so large-chip coordinates (>1000 um) and tiny SI PCell params (e.g. 6e-8)
     never suffer 6-digit %g truncation or O(N^2) strcat bloat. */
  let((first item pair key keyText)
    cond(
      (listp(value) && eq(car(value) 'jsonNull)
        fprintf(port "null"))
      (listp(value) && eq(car(value) 'jsonBool)
        fprintf(port "%s" if(cadr(value) "true" "false")))
      (listp(value) && eq(car(value) 'jsonArray)
        fprintf(port "[")
        first = t
        foreach(item cdr(value)
          unless(first fprintf(port ","))
          oagitLayoutExportWriteStreamValue(port item)
          first = nil
        )
        fprintf(port "]"))
      (listp(value) && eq(car(value) 'jsonObject)
        fprintf(port "{")
        first = t
        foreach(pair cdr(value)
          unless(first fprintf(port ","))
          key = car(pair)
          keyText = if(stringp(key) key sprintf(nil "%s" key))
          fprintf(port "\"%s\":" oagitJsonEscape(keyText))
          oagitLayoutExportWriteStreamValue(port cadr(pair))
          first = nil
        )
        fprintf(port "}"))
      (stringp(value)
        fprintf(port "\"%s\"" oagitJsonEscape(value)))
      (integerp(value)
        fprintf(port "%d" value))
      (floatp(value)
        if(oagitJsonFiniteFloatP(value)
            fprintf(port "%.12g" value)
            error("cannot encode a non-finite JSON number")))
      (t fprintf(port "%s" oagitJsonEncode(value)))
    )
  )
)

procedure(oagitLayoutExportWriteAtomicJson(path value)
  let((tmpPath port ok)
    ok = nil
    when(path
      tmpPath = strcat(path ".tmp")
      port = outfile(tmpPath "w")
      when(port
        oagitLayoutExportWriteStreamValue(port value)
        close(port)
        if(renameFile(tmpPath path)
            then ok = t
            else
              errset(deleteFile(tmpPath) t)
              ok = oagitWriteJsonFile(path value))
      )
    )
    ok
  )
)

procedure(oagitLayoutExportWithEnvelope(req pairs)
  let((out proto)
    out = pairs
    when(req && req->request_id
      proto = if(req->protocol oagitLayoutExportString(req->protocol) "oagit-spool/1")
      out = cons(list("protocol" proto)
                 cons(list("request_id" oagitLayoutExportString(req->request_id)) out)))
    cons('jsonObject out)
  )
)

procedure(oagitLayoutExportFail(req code message)
  let((outPath)
    outPath = req && req->output_path
    when(outPath
      errset(
        oagitLayoutExportWriteAtomicJson(
          outPath
          oagitLayoutExportWithEnvelope(
            req
            list(list("ok" list('jsonBool nil))
                 list("error" message)
                 list("error_code" code))))
        t))
    nil
  )
)

procedure(oagitLayoutExportRun(req)
  let((outPath lib cell view cv openedByScript captureResult response wrapped)
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
        oagitLayoutExportFail(req "open_failed" "unable to open requested layout CellView"))
      (cv~>modifiedButNotSaved
        oagitLayoutExportClose(cv openedByScript)
        oagitLayoutExportFail(req "modified_but_not_saved"
                              "CellView has unsaved modifications"))
      (t
        captureResult = errset(
          oagitLayoutExportBuildResponse(cv lib cell view)
          t)
        if(captureResult
            then
              response = car(captureResult)
              wrapped = oagitLayoutExportWithEnvelope(req cdr(response))
              errset(oagitLayoutExportWriteAtomicJson(outPath wrapped) t)
              oagitLayoutExportClose(cv openedByScript)
              wrapped
            else
              oagitLayoutExportClose(cv openedByScript)
              oagitLayoutExportFail(
                req "capture_failed"
                sprintf(nil "unexpected layout capture error: %L" errset.errset))))
    )
  )
)

procedure(oagitLayoutRunExport(req)
  /* Public Track 1 daemon.il entrypoint alias for capture_layout. */
  oagitLayoutExportRun(req)
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
     oagitRequest->action == "capture_layout"
  oagitLayoutExportRun(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
