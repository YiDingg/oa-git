/* Atomic draft-and-publish symbol restore for the oa-git bridge (IC26.1 schematicSymbol). */

procedure(oagitSymResString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitSymResMaybeString(value)
  if(value
      then oagitSymResString(value)
      else list('jsonNull))
)

procedure(oagitSymResBool(value)
  list('jsonBool if(eq(value t) t nil))
)

procedure(oagitSymResSetFailure(code message)
  unless(oagitSymResErrorCode
    oagitSymResErrorCode = code
    oagitSymResErrorMessage = message
  )
  nil
)

procedure(oagitSymResWriteResponse(req ok published pinVerified mismatches)
  let((target lib cell view draft)
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft = req->draft_cell
    errset(
      oagitWriteJsonFile(
        req->output_path
        list('jsonObject
             list("ok" oagitSymResBool(ok))
             list("error" if(ok list('jsonNull)
                                oagitSymResMaybeString(oagitSymResErrorMessage)))
             list("error_code" if(ok list('jsonNull)
                                     oagitSymResMaybeString(oagitSymResErrorCode)))
             list("target"
                  list('jsonObject
                       list("library" oagitSymResString(lib))
                       list("cell" oagitSymResString(cell))
                       list("view" oagitSymResString(view))))
             list("published" oagitSymResBool(published))
             list("draft_cell" oagitSymResString(draft))
             list("pin_contract_verified" oagitSymResBool(pinVerified))
             list("readback_verified" list('jsonBool nil))
             list("pin_mismatches" cons('jsonArray mismatches)))
      )
      t
    )
  )
)

procedure(oagitSymResFail(req code message)
  oagitSymResSetFailure(code message)
  oagitSymResWriteResponse(req nil nil nil nil)
  nil
)

procedure(oagitSymResOpenMaster(lib cell view)
  let((master openedByScript)
    master = dbFindOpenCellViewByName(lib cell view)
    if(master
        then openedByScript = nil
        else
          master = dbOpenCellViewByType(lib cell view nil "r")
          openedByScript = t)
    list(master openedByScript)
  )
)

procedure(oagitSymResCloseMaster(master openedByScript)
  when(master && openedByScript
    errset(dbClose(master) t)
  )
)

procedure(oagitSymResDefaultPinBBox(xy)
  let((x y)
    x = float(car(xy))
    y = float(cadr(xy))
    list(list(x - 0.025 y - 0.025) list(x + 0.025 y + 0.025))
  )
)

procedure(oagitSymResCreatePins(cv pinRecords)
  let((rec pinName dir xy orient pf pfKind pfLayer pfPurpose
       bbox net term fig pinObj masterInfo master openedByScript)
    foreach(rec pinRecords
      unless(oagitSymResErrorCode
        pinName = oagitSymResString(rec->name)
        dir = if(rec->direction oagitSymResString(rec->direction) "inputOutput")
        xy = rec->xy
        orient = if(rec->orient oagitSymResString(rec->orient) "R0")
        pf = rec->pin_figure
        pfKind = if(pf && pf->kind oagitSymResString(pf->kind) "rect")
        pfLayer = if(pf && pf->layer oagitSymResString(pf->layer) "pin")
        pfPurpose = if(pf && pf->purpose oagitSymResString(pf->purpose) "drawing")

        net = dbFindNetByName(cv pinName)
        unless(net net = dbMakeNet(cv pinName))
        unless(net
          oagitSymResSetFailure("pin_create_failed"
                                sprintf(nil "dbMakeNet failed for pin %s" pinName)))

        when(net && !oagitSymResErrorCode
          term = dbFindTermByName(cv pinName)
          unless(term term = dbCreateTerm(net pinName dir))
          unless(term
            oagitSymResSetFailure("pin_create_failed"
                                  sprintf(nil "dbCreateTerm failed for pin %s" pinName)))
        )

        when(term && !oagitSymResErrorCode
          fig = nil
          cond(
            (equal(pfKind "rect") || equal(pfKind "rectangle")
              bbox = if(pf && pf->bbox pf->bbox oagitSymResDefaultPinBBox(xy))
              fig = dbCreateRect(cv list(pfLayer pfPurpose) bbox))
            (equal(pfKind "ellipse") || equal(pfKind "circle")
              bbox = if(pf && pf->bbox pf->bbox oagitSymResDefaultPinBBox(xy))
              fig = dbCreateEllipse(cv list(pfLayer pfPurpose) bbox))
            (equal(pfKind "polygon")
              if(pf && pf->points
                  then fig = dbCreatePolygon(cv list(pfLayer pfPurpose) pf->points)
                  else
                    bbox = if(pf && pf->bbox pf->bbox oagitSymResDefaultPinBBox(xy))
                    fig = dbCreateRect(cv list(pfLayer pfPurpose) bbox)))
            (equal(pfKind "inst")
              masterInfo = oagitSymResOpenMaster(pf->lib pf->cell pf->view)
              master = car(masterInfo)
              openedByScript = cadr(masterInfo)
              if(master
                  then
                    fig = dbCreateInst(cv master nil xy orient)
                    oagitSymResCloseMaster(master openedByScript)
                  else
                    oagitSymResSetFailure("pin_master_open_failed"
                                          sprintf(nil "unable to open pin master for %s" pinName))))
            (t
              oagitSymResSetFailure("unsupported_object"
                                    sprintf(nil "unsupported pin_figure kind %s" pfKind)))
          )
          when(!oagitSymResErrorCode
            unless(fig
              oagitSymResSetFailure("pin_create_failed"
                                    sprintf(nil "unable to create pin figure for %s" pinName))))
          when(fig && !oagitSymResErrorCode
            pinObj = dbCreatePin(net fig pinName term)
            unless(pinObj
              oagitSymResSetFailure("pin_create_failed"
                                    sprintf(nil "dbCreatePin failed for %s" pinName))))
        )
      )
    )
    !oagitSymResErrorCode
  )
)

procedure(oagitSymResCreateSelectionBox(cv graphics)
  let((sb fig)
    sb = graphics && graphics->selection_box
    when(sb && !oagitSymResErrorCode
      fig = dbCreateRect(cv list("instance" "drawing") sb)
      unless(fig
        oagitSymResSetFailure("shape_create_failed"
                              "dbCreateRect failed for instance:drawing selection_box"))
    )
    !oagitSymResErrorCode
  )
)

procedure(oagitSymResCreateShapes(cv shapeRecords)
  let((rec kind lpp width fig)
    foreach(rec shapeRecords
      unless(oagitSymResErrorCode
        kind = oagitSymResString(rec->kind)
        lpp = list(oagitSymResString(rec->layer)
                   if(rec->purpose oagitSymResString(rec->purpose) "drawing"))
        fig = nil
        cond(
          (equal(kind "line")
            fig = dbCreateLine(cv lpp rec->points))
          (equal(kind "rect") || equal(kind "rectangle")
            fig = dbCreateRect(cv lpp rec->bbox))
          (equal(kind "polygon")
            fig = dbCreatePolygon(cv lpp rec->points))
          (equal(kind "path")
            width = if(numberp(rec->width) float(rec->width) 0.0)
            fig = dbCreatePath(cv lpp rec->points width))
          (equal(kind "arc")
            fig = dbCreateArc(cv lpp rec->ellipse_bbox rec->arc_bbox))
          (equal(kind "ellipse") || equal(kind "circle")
            fig = dbCreateEllipse(cv lpp rec->bbox))
          (t
            oagitSymResSetFailure("unsupported_object"
                                  sprintf(nil "unsupported symbol shape kind %s" kind)))
        )
        when(!oagitSymResErrorCode && !fig
          oagitSymResSetFailure("shape_create_failed"
                                sprintf(nil "failed to create symbol shape kind=%s" kind)))
      )
    )
    !oagitSymResErrorCode
  )
)

procedure(oagitSymResCreateLabelList(cv records defaultLabelType)
  let((rec lpp height lbl lType)
    foreach(rec records
      unless(oagitSymResErrorCode
        lpp = list(if(rec->layer oagitSymResString(rec->layer) "pin")
                   if(rec->purpose oagitSymResString(rec->purpose) "label"))
        height = if(numberp(rec->height) float(rec->height) 0.0625)
        lbl = dbCreateLabel(
          cv lpp rec->xy oagitSymResString(rec->text)
          if(rec->justify oagitSymResString(rec->justify) "centerCenter")
          if(rec->orient oagitSymResString(rec->orient) "R0")
          if(rec->font oagitSymResString(rec->font) "stick")
          height)
        unless(lbl
          oagitSymResSetFailure("label_create_failed"
                                "dbCreateLabel returned nil"))
        when(lbl && !oagitSymResErrorCode
          lType = if(rec->label_type oagitSymResString(rec->label_type) defaultLabelType)
          when(equal(lType "NLPLabel") || equal(lType "ILLabel")
            lbl~>labelType = lType))
      )
    )
    !oagitSymResErrorCode
  )
)

procedure(oagitSymResCreateLabels(cv graphics)
  when(graphics && !oagitSymResErrorCode
    oagitSymResCreateLabelList(cv graphics->labels "normalLabel")
    unless(oagitSymResErrorCode
      oagitSymResCreateLabelList(cv graphics->nlp_labels "NLPLabel"))
    unless(oagitSymResErrorCode
      oagitSymResCreateLabelList(cv graphics->il_labels "ILLabel"))
  )
  !oagitSymResErrorCode
)

procedure(oagitSymResScalarListP(value)
  let((ok cur elem)
    if(value == nil
        then t
        else
          ok = listp(value)
          cur = value
          while(ok && dtpr(cur)
            elem = car(cur)
            unless(stringp(elem) || integerp(elem) || floatp(elem) ||
                   eq(elem t) || eq(elem nil)
              ok = nil)
            cur = cdr(cur)
          )
          when(cur != nil
            ok = nil)
          ok)
  )
)

procedure(oagitSymResPropValue(value propType)
  let((out elem convElem)
    cond(
      (equal(propType "string")
        if(stringp(value) value oagitSymResString(value)))
      (equal(propType "int")
        if(integerp(value) value if(numberp(value) fix(value) nil)))
      (equal(propType "float")
        if(numberp(value) float(value) nil))
      (equal(propType "boolean")
        if(value t nil))
      (equal(propType "ILList")
        out = nil
        foreach(elem value
          convElem = cond(
            (eq(elem t) t)
            (eq(elem nil) nil)
            (stringp(elem) elem)
            (integerp(elem) elem)
            (floatp(elem) float(elem))
            (t oagitSymResString(elem))
          )
          out = cons(convElem out)
        )
        reverse(out))
      (t nil)
    )
  )
)

procedure(oagitSymResCreateProperties(cv req)
  let((portList item triple pName pVal pType converted propRes)
    when(req->port_order && !oagitSymResErrorCode
      portList = nil
      foreach(item req->port_order
        portList = cons(oagitSymResString(item) portList))
      portList = reverse(portList)
      dbDeletePropByName(cv "portOrder")
      propRes = dbCreateProp(cv "portOrder" "ILList" portList)
      unless(propRes
        oagitSymResSetFailure("property_create_failed"
                              "dbCreateProp failed for portOrder"))
    )
    foreach(triple req->properties
      unless(oagitSymResErrorCode
        pName = oagitSymResString(car(triple))
        pVal = cadr(triple)
        pType = oagitSymResString(caddr(triple))
        unless(member(pType list("string" "int" "float" "boolean" "ILList"))
          oagitSymResSetFailure("property_error"
                                sprintf(nil "unsupported property type %s" pType)))
        unless(oagitSymResErrorCode
          if(equal(pType "ILList")
              then
                if(oagitSymResScalarListP(pVal)
                    then converted = oagitSymResPropValue(pVal pType)
                    else
                      oagitSymResSetFailure(
                        "property_error"
                        sprintf(nil "invalid ILList property value for %s" pName)))
              else
                converted = oagitSymResPropValue(pVal pType)
                unless(equal(pType "boolean") || converted != nil
                  oagitSymResSetFailure("property_error"
                                        sprintf(nil "invalid property value for %s" pName))))
        )
        unless(oagitSymResErrorCode
          dbDeletePropByName(cv pName)
          propRes = dbCreateProp(cv pName pType converted)
          unless(propRes
            oagitSymResSetFailure("property_create_failed"
                                  sprintf(nil "dbCreateProp failed for %s" pName)))
        )
      )
    )
    !oagitSymResErrorCode
  )
)

procedure(oagitSymResMismatch(pinName expected actual reason)
  list('jsonObject
       list("pin" oagitSymResString(pinName))
       list("expected" oagitSymResMaybeString(expected))
       list("actual" oagitSymResMaybeString(actual))
       list("reason" reason))
)

procedure(oagitSymResVerifyPins(cv pinRecords)
  let((expectedByName rec pinName expectedDir term actualDir termName)
    oagitSymResMismatches = nil
    expectedByName = makeTable("oagitSymExpectedPins" nil)
    foreach(rec pinRecords
      pinName = oagitSymResString(rec->name)
      expectedDir = if(rec->direction oagitSymResString(rec->direction) "inputOutput")
      expectedByName[pinName] = expectedDir
      term = dbFindTermByName(cv pinName)
      cond(
        (!term
          oagitSymResMismatches = cons(
            oagitSymResMismatch(pinName expectedDir nil "missing_terminal")
            oagitSymResMismatches))
        (!term~>pins
          oagitSymResMismatches = cons(
            oagitSymResMismatch(pinName expectedDir nil "missing_pin_figure")
            oagitSymResMismatches))
        (t
          actualDir = if(term~>direction oagitSymResString(term~>direction) "inputOutput")
          unless(equal(expectedDir actualDir)
            oagitSymResMismatches = cons(
              oagitSymResMismatch(pinName expectedDir actualDir "direction_mismatch")
              oagitSymResMismatches)))
      )
    )
    foreach(term cv~>terminals
      termName = oagitSymResString(term~>name)
      unless(expectedByName[termName]
        oagitSymResMismatches = cons(
          oagitSymResMismatch(termName nil oagitSymResString(term~>direction)
                              "unexpected_terminal")
          oagitSymResMismatches))
    )
    oagitSymResMismatches = reverse(oagitSymResMismatches)
    if(oagitSymResMismatches
        then
          oagitSymResSetFailure("pin_contract_mismatch"
                                "restored symbol terminals do not match expected pin contract")
          nil
        else
          oagitSymResPinsVerified = t
          t)
  )
)

procedure(oagitSymResCleanupDraft(lib draft_cell view cv)
  let((purgeResult draftDd deleteResult cleanupOk)
    cleanupOk = t
    when(cv
      purgeResult = errset(dbPurge(cv) t)
      unless(purgeResult && car(purgeResult)
        cleanupOk = nil
        oagitSymResErrorCode = "cleanup_failed"
        oagitSymResErrorMessage = "dbPurge failed during symbol draft cleanup"
      )
    )
    draftDd = ddGetObj(lib draft_cell)
    when(draftDd
      deleteResult = errset(ddDeleteObj(draftDd) t)
      unless(deleteResult && car(deleteResult)
        cleanupOk = nil
        oagitSymResErrorCode = "cleanup_failed"
        oagitSymResErrorMessage = "ddDeleteObj failed during symbol draft cleanup"
      )
    )
    cleanupOk
  )
)

procedure(oagitSymResBuild(req cv)
  let((graphics)
    graphics = req->graphics
    oagitSymResCreatePins(cv req->pins)
    unless(oagitSymResErrorCode
      oagitSymResCreateSelectionBox(cv graphics))
    unless(oagitSymResErrorCode
      when(graphics && graphics->shapes
        oagitSymResCreateShapes(cv graphics->shapes)))
    unless(oagitSymResErrorCode
      oagitSymResCreateLabels(cv graphics))
    unless(oagitSymResErrorCode
      oagitSymResCreateProperties(cv req))
    unless(oagitSymResErrorCode
      oagitSymResVerifyPins(cv req->pins))
    unless(oagitSymResErrorCode
      oagitSymResSaveResult = errset(dbSave(cv) t)
      unless(oagitSymResSaveResult && car(oagitSymResSaveResult)
        oagitSymResSetFailure("save_failed" "dbSave failed for symbol draft")))
    !oagitSymResErrorCode
  )
)

procedure(oagitSymRunRestore(req)
  let((target lib cell view draft_cell overwrite cv
       buildResult copyResult pubCv stillOpen cleanupResult)
    oagitSymResErrorCode = nil
    oagitSymResErrorMessage = nil
    oagitSymResMismatches = nil
    oagitSymResPinsVerified = nil
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft_cell = req->draft_cell
    overwrite = req->overwrite
    cond(
      (!member(lib req->allow_libraries)
        oagitSymResFail(req "disallowed_library"
                        "target library is not allow-listed"))
      (oagitDirHasLiveLocks(lib cell view)
        oagitSymResFail(req "live_lock"
                        "target symbol view has a live edit lock"))
      (ddGetObj(lib cell view) && !overwrite
        oagitSymResFail(req "target_exists"
                        "target symbol CellView already exists"))
      (equal(draft_cell cell)
        oagitSymResFail(req "draft_exists"
                        "draft cell must be distinct from target cell"))
      (ddGetObj(lib draft_cell view)
        oagitSymResFail(req "draft_exists"
                        "draft symbol CellView already exists"))
      (t
        cv = dbOpenCellViewByType(lib draft_cell view "schematicSymbol" "w")
        unless(cv
          oagitSymResFail(req "draft_open_failed"
                          "unable to open draft symbol CellView"))
        when(cv
          buildResult = errset(oagitSymResBuild(req cv) t)
          unless(buildResult
            oagitSymResSetFailure(
              "restore_failed"
              sprintf(nil "unexpected symbol restore error: %L" errset.errset))
          )
          if(oagitSymResErrorCode
              then
                oagitSymResCleanupDraft(lib draft_cell view cv)
                oagitSymResWriteResponse(
                  req nil nil oagitSymResPinsVerified oagitSymResMismatches)
              else
                copyResult = errset(
                  dbCopyCellView(cv lib cell view nil nil t)
                  t)
                pubCv = nil
                unless(copyResult && car(copyResult)
                  oagitSymResSetFailure("publish_failed"
                                        "dbCopyCellView failed for symbol"))
                if(oagitSymResErrorCode
                    then
                      oagitSymResCleanupDraft(lib draft_cell view cv)
                      oagitSymResWriteResponse(
                        req nil nil oagitSymResPinsVerified oagitSymResMismatches)
                    else
                      pubCv = car(copyResult)
                      when(pubCv
                        errset(dbClose(pubCv) t)
                        errset(dbPurge(pubCv) t)
                      )
                      stillOpen = dbFindOpenCellViewByName(lib cell view)
                      when(stillOpen
                        errset(dbClose(stillOpen) t)
                        errset(dbPurge(stillOpen) t)
                      )
                      cleanupResult = oagitSymResCleanupDraft(
                        lib draft_cell view cv)
                      unless(cleanupResult
                        unless(oagitSymResErrorCode
                          oagitSymResSetFailure(
                            "cleanup_failed" "symbol draft cleanup failed")))
                      if(oagitSymResErrorCode
                          then
                            oagitSymResWriteResponse(
                              req nil nil oagitSymResPinsVerified
                              oagitSymResMismatches)
                          else
                            oagitSymResWriteResponse(
                              req t t oagitSymResPinsVerified nil))
                  )
              )
        ))
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
     oagitRequest->action == "restore_symbol"
  oagitSymRunRestore(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
