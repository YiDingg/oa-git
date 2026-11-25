/* Atomic draft-and-publish layout restore for the oa-git bridge. */

procedure(oagitLayoutRestoreString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitLayoutRestoreMaybeString(value)
  let((text)
    if(value
        then
          text = oagitLayoutRestoreString(value)
          if(strlen(text) > 0 text list('jsonNull))
        else list('jsonNull))
  )
)

procedure(oagitLayoutRestoreBoolNorm(value)
  if(equal(value t) || equal(value "TRUE") || equal(value "true")
      then t
      else nil)
)

procedure(oagitLayoutRestoreBool(value)
  list('jsonBool if(eq(value t) t nil))
)

procedure(oagitLayoutRestoreScalar(value)
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

procedure(oagitLayoutRestoreJsonVal(value paramType)
  if(equal(paramType "boolean")
      then list('jsonBool oagitLayoutRestoreBoolNorm(value))
      else oagitLayoutRestoreScalar(value))
)

procedure(oagitLayoutRestoreInt(value defaultVal)
  cond(
    (integerp(value) value)
    (floatp(value) && oagitJsonFiniteFloatP(value) fix(value))
    (t defaultVal)
  )
)

procedure(oagitLayoutRestoreFloat(value defaultVal)
  cond(
    (floatp(value) && oagitJsonFiniteFloatP(value) value)
    (integerp(value) float(value))
    (t defaultVal)
  )
)

procedure(oagitLayoutRestoreSetFailure(code message)
  unless(oagitLayoutRestoreErrorCode
    oagitLayoutRestoreErrorCode = code
    oagitLayoutRestoreErrorMessage = message
  )
  nil
)

procedure(oagitLayoutRestoreCountsObject()
  list('jsonObject
       list("nets" oagitLayoutRestoreCountNets)
       list("boundaries" oagitLayoutRestoreCountBoundaries)
       list("instances" oagitLayoutRestoreCountInstances)
       list("mosaics" oagitLayoutRestoreCountMosaics)
       list("vias" oagitLayoutRestoreCountVias)
       list("shapes" oagitLayoutRestoreCountShapes)
       list("pins" oagitLayoutRestoreCountPins)
       list("labels" oagitLayoutRestoreCountLabels)
       list("blockages" oagitLayoutRestoreCountBlockages)
       list("fig_groups" oagitLayoutRestoreCountFigGroups))
)

procedure(oagitLayoutRestoreWriteAtomicJson(path value)
  let((tmpPath port ok)
    ok = nil
    when(path
      tmpPath = strcat(path ".tmp")
      port = outfile(tmpPath "w")
      when(port
        fprintf(port "%s" oagitJsonEncode(value))
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

procedure(oagitLayoutRestoreWriteResponse(req ok published)
  let((target lib cell view draft pairs proto)
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft = req->draft_cell
    pairs = list(
      list("ok" oagitLayoutRestoreBool(ok))
      list("error" if(ok list('jsonNull)
                          if(oagitLayoutRestoreErrorCode
                              oagitLayoutRestoreErrorMessage
                              list('jsonNull))))
      list("error_code" if(ok list('jsonNull)
                               if(oagitLayoutRestoreErrorCode
                                   oagitLayoutRestoreErrorCode
                                   list('jsonNull))))
      list("target"
           list('jsonObject
                list("library" oagitLayoutRestoreString(lib))
                list("cell" oagitLayoutRestoreString(cell))
                list("view" oagitLayoutRestoreString(view))))
      list("published" oagitLayoutRestoreBool(published))
      list("draft_cell" oagitLayoutRestoreString(draft))
      list("pcell_verified" oagitLayoutRestoreBool(oagitLayoutRestorePCellVerified))
      list("connectivity_verified" oagitLayoutRestoreBool(oagitLayoutRestoreConnVerified))
      list("object_counts" oagitLayoutRestoreCountsObject())
      list("pcell_mismatches" cons('jsonArray oagitLayoutRestorePCellMismatches))
      list("connectivity_mismatches" cons('jsonArray oagitLayoutRestoreConnMismatches)))
    when(req && req->request_id
      proto = if(req->protocol oagitLayoutRestoreString(req->protocol) "oagit-spool/1")
      pairs = cons(list("protocol" proto)
                   cons(list("request_id" oagitLayoutRestoreString(req->request_id)) pairs)))
    errset(
      oagitLayoutRestoreWriteAtomicJson(
        req->output_path
        cons('jsonObject pairs)
      )
      t
    )
  )
)

procedure(oagitLayoutRestoreFail(req code message)
  oagitLayoutRestoreSetFailure(code message)
  oagitLayoutRestoreWriteResponse(req nil nil)
  nil
)

procedure(oagitLayoutRestoreOpenMaster(lib cell view)
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

procedure(oagitLayoutRestoreCloseMaster(master openedByScript)
  when(master && openedByScript
    errset(dbClose(master) t)
  )
)

procedure(oagitLayoutRestoreInferType(value)
  cond(
    (stringp(value) "string")
    (integerp(value) "int")
    (floatp(value) "float")
    (t "boolean")
  )
)

procedure(oagitLayoutRestoreDbParamType(paramType)
  cond(
    (equal(paramType "string") "string")
    (equal(paramType "cyclic") "string")
    (equal(paramType "radio") "string")
    (equal(paramType "int") "int")
    (equal(paramType "float") "float")
    (equal(paramType "boolean") "boolean")
    (t nil)
  )
)

procedure(oagitLayoutRestoreParamValue(value paramType)
  let((parsed)
    cond(
      (equal(paramType "string") oagitLayoutRestoreString(value))
      (equal(paramType "int")
        cond(
          (integerp(value) value)
          (numberp(value) fix(value))
          (stringp(value) atoi(value))
          (t nil)))
      (equal(paramType "float")
        cond(
          (floatp(value) value)
          (integerp(value) float(value))
          (stringp(value)
            parsed = errset(cdfParseFloatString(value) t)
            if(parsed && numberp(car(parsed)) float(car(parsed)) nil))
          (t nil)))
      (equal(paramType "boolean")
        if(value
            then
              if(equal(value "nil") || equal(value "false") || equal(value "FALSE")
                  nil
                  t)
            else nil))
      (t nil)
    )
  )
)

procedure(oagitLayoutRestoreApplyProps(obj props)
  let((triple pName pValue rawType propType converted propResult)
    foreach(triple props
      unless(oagitLayoutRestoreErrorCode
        pName = oagitLayoutRestoreString(car(triple))
        pValue = cadr(triple)
        rawType = if(length(triple) >= 3
                      oagitLayoutRestoreString(caddr(triple))
                      oagitLayoutRestoreInferType(pValue))
        propType = oagitLayoutRestoreDbParamType(rawType)
        unless(propType && member(propType list("string" "int" "float" "boolean"))
          oagitLayoutRestoreSetFailure(
            "property_error"
            sprintf(nil "unsupported property type %s for %s" rawType pName)))
        when(!oagitLayoutRestoreErrorCode
          converted = oagitLayoutRestoreParamValue(pValue propType)
          unless(equal(propType "boolean") || converted != nil
            oagitLayoutRestoreSetFailure(
              "property_error"
              sprintf(nil "invalid property value for %s" pName)))
          unless(oagitLayoutRestoreErrorCode
            propResult = errset(dbCreateProp(obj pName propType converted) t)
            unless(propResult && car(propResult)
              oagitLayoutRestoreSetFailure(
                "property_create_failed"
                sprintf(nil "dbCreateProp failed for %s" pName))))
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreBuildParamList(pcellParams)
  let((out triple pName pValue rawType dbType converted)
    out = nil
    foreach(triple pcellParams
      unless(oagitLayoutRestoreErrorCode
        pName = oagitLayoutRestoreString(car(triple))
        pValue = cadr(triple)
        rawType = if(length(triple) >= 3
                      oagitLayoutRestoreString(caddr(triple))
                      oagitLayoutRestoreInferType(pValue))
        dbType = oagitLayoutRestoreDbParamType(rawType)
        unless(dbType
          oagitLayoutRestoreSetFailure(
            "pcell_parameter_error"
            sprintf(nil "unsupported PCell parameter type %s for %s" rawType pName)))
        when(!oagitLayoutRestoreErrorCode
          converted = oagitLayoutRestoreParamValue(pValue dbType)
          when(equal(dbType "float") && !numberp(converted)
            oagitLayoutRestoreSetFailure(
              "pcell_parameter_error"
              sprintf(nil "invalid float PCell parameter value for %s" pName)))
          when(equal(dbType "int") && !integerp(converted)
            oagitLayoutRestoreSetFailure(
              "pcell_parameter_error"
              sprintf(nil "invalid int PCell parameter value for %s" pName)))
          unless(oagitLayoutRestoreErrorCode
            out = cons(list(pName dbType converted) out))
        )
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutRestoreStringList(items)
  let((out item text)
    out = nil
    foreach(item items
      text = oagitLayoutRestoreString(item)
      when(strlen(text) > 0
        out = cons(text out))
    )
    reverse(out)
  )
)

procedure(oagitLayoutRestoreCreateNets(cv records)
  let((record netName sigType net)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        netName = oagitLayoutRestoreString(record->name)
        net = dbMakeNet(cv netName)
        unless(net
          oagitLayoutRestoreSetFailure(
            "net_create_failed"
            sprintf(nil "dbMakeNet failed for %s" netName)))
        when(net && !oagitLayoutRestoreErrorCode
          sigType = if(record->sig_type
                        oagitLayoutRestoreString(record->sig_type)
                        "signal")
          when(strlen(sigType) > 0 && !equal(sigType "signal")
            errset(net~>sigType = sigType t))
          oagitLayoutRestoreApplyProps(net record->props)
          oagitLayoutRestoreCountNets = oagitLayoutRestoreCountNets + 1
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateBoundaries(cv records)
  let((record kind pts bName cluster bRes bnd)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        kind = if(record->boundary_kind
                   oagitLayoutRestoreString(record->boundary_kind)
                   "prBoundary")
        pts = record->points
        bName = if(record->name && strlen(oagitLayoutRestoreString(record->name)) > 0
                    oagitLayoutRestoreString(record->name)
                    nil)
        bRes = cond(
          (equal(kind "prBoundary")
            errset(dbCreatePRBoundary(cv pts) t))
          (equal(kind "areaBoundary")
            if(bName
                errset(dbCreateAreaBoundary(cv pts bName) t)
                errset(dbCreateAreaBoundary(cv pts) t)))
          (equal(kind "snapBoundary")
            errset(dbCreateSnapBoundary(cv pts) t))
          (equal(kind "clusterBoundary")
            let((cName)
              cName = if(bName bName "oagitCluster")
              cluster = dbFindClusterByName(cv cName)
              unless(cluster
                cluster = dbCreateCluster(cv cName "inclusive"))
              if(cluster
                  errset(dbCreateClusterBoundary(cluster cName pts) t)
                  nil)))
          (t
            oagitLayoutRestoreSetFailure(
              "unsupported_object"
              sprintf(nil "unsupported boundary kind %s" kind))
            nil)
        )
        unless(oagitLayoutRestoreErrorCode
          bnd = if(bRes car(bRes) nil)
          unless(bnd
            oagitLayoutRestoreSetFailure(
              "boundary_create_failed"
              sprintf(nil "failed to create %s" kind)))
          when(bnd && !oagitLayoutRestoreErrorCode
            oagitLayoutRestoreApplyProps(bnd record->props)
            oagitLayoutRestoreCountBoundaries = oagitLayoutRestoreCountBoundaries + 1
          )
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateInstances(cv records instByName)
  let((record mLib mCell mView instName xy orient masterInfo master
       openedByScript paramList instRes inst pair termName netName net connRes)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        instName = oagitLayoutRestoreString(record->name)
        mLib = oagitLayoutRestoreString(record->lib)
        mCell = oagitLayoutRestoreString(record->cell)
        mView = if(record->view oagitLayoutRestoreString(record->view) "layout")
        xy = if(record->xy record->xy list(0.0 0.0))
        orient = if(record->orient oagitLayoutRestoreString(record->orient) "R0")
        masterInfo = oagitLayoutRestoreOpenMaster(mLib mCell mView)
        master = car(masterInfo)
        openedByScript = cadr(masterInfo)
        unless(master
          oagitLayoutRestoreSetFailure(
            "master_open_failed"
            sprintf(nil "unable to open instance master %s/%s/%s" mLib mCell mView)))
        when(master && !oagitLayoutRestoreErrorCode
          if(record->is_pcell && record->pcell_params
              then
                paramList = oagitLayoutRestoreBuildParamList(record->pcell_params)
                unless(oagitLayoutRestoreErrorCode
                  instRes = errset(
                    dbCreateParamInst(cv master instName xy orient 1 paramList)
                    t))
              else
                instRes = errset(
                  dbCreateInst(cv master instName xy orient 1)
                  t))
          unless(oagitLayoutRestoreErrorCode
            inst = if(instRes car(instRes) nil)
            unless(inst
              oagitLayoutRestoreSetFailure(
                "instance_create_failed"
                sprintf(nil "failed to create instance %s" instName)))
            when(inst && !oagitLayoutRestoreErrorCode
              instByName[instName] = inst
              oagitLayoutRestoreCountInstances = oagitLayoutRestoreCountInstances + 1
              oagitLayoutRestoreApplyProps(inst record->props)
              foreach(pair record->connections
                unless(oagitLayoutRestoreErrorCode
                  termName = oagitLayoutRestoreString(car(pair))
                  netName = oagitLayoutRestoreString(cadr(pair))
                  net = dbMakeNet(cv netName)
                  unless(net
                    oagitLayoutRestoreSetFailure(
                      "connectivity_create_failed"
                      sprintf(nil "dbMakeNet failed for %s" netName)))
                  when(net && !oagitLayoutRestoreErrorCode
                    connRes = errset(dbCreateConnByName(net inst termName) t)
                    unless(connRes && car(connRes)
                      oagitLayoutRestoreSetFailure(
                        "connectivity_create_failed"
                        sprintf(nil "dbCreateConnByName failed for %s.%s -> %s"
                                instName termName netName))))
                )
              )
            )
          )
        )
        oagitLayoutRestoreCloseMaster(master openedByScript)
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateMosaics(cv records mosaicByName)
  let((record mName mLib mCell mView xy orient rows cols uX uY
       masterInfo master openedByScript paramList mRes mosaic)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        mName = oagitLayoutRestoreString(record->name)
        mLib = oagitLayoutRestoreString(record->lib)
        mCell = oagitLayoutRestoreString(record->cell)
        mView = if(record->view oagitLayoutRestoreString(record->view) "layout")
        xy = if(record->xy record->xy list(0.0 0.0))
        orient = if(record->orient oagitLayoutRestoreString(record->orient) "R0")
        rows = oagitLayoutRestoreInt(record->rows 1)
        cols = oagitLayoutRestoreInt(record->columns 1)
        uX = oagitLayoutRestoreFloat(record->u_x 0.0)
        uY = oagitLayoutRestoreFloat(record->u_y 0.0)
        masterInfo = oagitLayoutRestoreOpenMaster(mLib mCell mView)
        master = car(masterInfo)
        openedByScript = cadr(masterInfo)
        unless(master
          oagitLayoutRestoreSetFailure(
            "master_open_failed"
            sprintf(nil "unable to open mosaic master %s/%s/%s" mLib mCell mView)))
        when(master && !oagitLayoutRestoreErrorCode
          if(record->is_pcell && record->pcell_params
              then
                paramList = oagitLayoutRestoreBuildParamList(record->pcell_params)
                unless(oagitLayoutRestoreErrorCode
                  mRes = errset(
                    dbCreateParamSimpleMosaic(cv master mName xy orient rows cols uY uX paramList)
                    t))
              else
                mRes = errset(
                  dbCreateSimpleMosaic(cv master mName xy orient rows cols uY uX)
                  t))
          unless(oagitLayoutRestoreErrorCode
            mosaic = if(mRes car(mRes) nil)
            unless(mosaic
              oagitLayoutRestoreSetFailure(
                "mosaic_create_failed"
                sprintf(nil "failed to create mosaic %s" mName)))
            when(mosaic && !oagitLayoutRestoreErrorCode
              mosaicByName[mName] = mosaic
              oagitLayoutRestoreCountMosaics = oagitLayoutRestoreCountMosaics + 1
              oagitLayoutRestoreApplyProps(mosaic record->props)
            )
          )
        )
        oagitLayoutRestoreCloseMaster(master openedByScript)
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreViaParams(rawParams)
  let((out pair key val)
    out = nil
    when(listp(rawParams)
      foreach(pair rawParams
        when(listp(pair) && car(pair)
          key = oagitLayoutRestoreString(car(pair))
          val = cadr(pair)
          when(strlen(key) > 0
            out = cons(list(key val) out))
        )
      )
    )
    reverse(out)
  )
)

procedure(oagitLayoutRestoreCreateVias(cv techFile records)
  let((record viaDefName viaDef origin orient overrideParams viaRes via
       netName net viaName)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        viaDefName = oagitLayoutRestoreString(record->via_def)
        unless(techFile
          oagitLayoutRestoreSetFailure(
            "via_def_missing"
            sprintf(nil "techFile is unavailable for viaDef %s" viaDefName)))
        when(techFile && !oagitLayoutRestoreErrorCode
          viaDef = techFindViaDefByName(techFile viaDefName)
          unless(viaDef
            oagitLayoutRestoreSetFailure(
              "via_def_missing"
              sprintf(nil "viaDef %s not found in techFile" viaDefName)))
          when(viaDef && !oagitLayoutRestoreErrorCode
            origin = if(record->origin record->origin list(0.0 0.0))
            orient = if(record->orient oagitLayoutRestoreString(record->orient) "R0")
            overrideParams = oagitLayoutRestoreViaParams(record->params)
            viaRes = errset(
              dbCreateVia(cv viaDef origin orient overrideParams)
              t)
            via = if(viaRes car(viaRes) nil)
            unless(via
              oagitLayoutRestoreSetFailure(
                "via_create_failed"
                sprintf(nil "dbCreateVia failed for %s" viaDefName)))
            when(via && !oagitLayoutRestoreErrorCode
              oagitLayoutRestoreCountVias = oagitLayoutRestoreCountVias + 1
              netName = if(record->net && strlen(oagitLayoutRestoreString(record->net)) > 0
                            oagitLayoutRestoreString(record->net)
                            nil)
              when(netName
                net = dbMakeNet(cv netName)
                unless(net && dbAddFigToNet(via net)
                  oagitLayoutRestoreSetFailure(
                    "connectivity_create_failed"
                    sprintf(nil "dbAddFigToNet failed for via net %s" netName))))
              viaName = if(record->name && strlen(oagitLayoutRestoreString(record->name)) > 0
                            oagitLayoutRestoreString(record->name)
                            nil)
              when(viaName && !oagitLayoutRestoreErrorCode
                errset(via~>name = viaName t))
              unless(oagitLayoutRestoreErrorCode
                oagitLayoutRestoreApplyProps(via record->props))
            )
          )
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreatePathSeg(cv lpp record)
  let((bStyle eStyle width bExt eExt customSpec styleArg sRes shape)
    bStyle = if(record->begin_style oagitLayoutRestoreString(record->begin_style) "truncate")
    eStyle = if(record->end_style oagitLayoutRestoreString(record->end_style) "truncate")
    width = oagitLayoutRestoreFloat(record->width 0.0)
    bExt = oagitLayoutRestoreFloat(record->begin_ext 0.0)
    eExt = oagitLayoutRestoreFloat(record->end_ext 0.0)
    customSpec = record->custom_style
    cond(
      (equal(bStyle "custom") || equal(eStyle "custom")
        styleArg = if(listp(customSpec) && length(customSpec) == 6
                       list(bExt eExt customSpec)
                       list(bExt eExt))
        sRes = errset(
          dbCreatePathSeg(cv lpp record->begin_pt record->end_pt width
                          bStyle eStyle styleArg)
          t)
        shape = if(sRes car(sRes) nil)
        unless(shape
          sRes = errset(
            dbCreatePathSeg(cv lpp record->begin_pt record->end_pt width
                            bStyle eStyle)
            t)
          shape = if(sRes car(sRes) nil)
          when(shape
            errset(shape~>beginExt = bExt t)
            errset(shape~>endExt = eExt t))))
      (equal(bStyle "variable") || equal(eStyle "variable")
        sRes = errset(
          dbCreatePathSeg(cv lpp record->begin_pt record->end_pt width
                          bStyle eStyle list(bExt eExt))
          t)
        shape = if(sRes car(sRes) nil)
        unless(shape
          sRes = errset(
            dbCreatePathSeg(cv lpp record->begin_pt record->end_pt width
                            bStyle eStyle)
            t)
          shape = if(sRes car(sRes) nil)
          when(shape
            errset(shape~>beginExt = bExt t)
            errset(shape~>endExt = eExt t))))
      (t
        sRes = errset(
          dbCreatePathSeg(cv lpp record->begin_pt record->end_pt width
                          bStyle eStyle)
          t)
        shape = if(sRes car(sRes) nil)
        when(shape && (bExt != 0.0 || eExt != 0.0)
          errset(shape~>beginExt = bExt t)
          errset(shape~>endExt = eExt t)))
    )
    shape
  )
)

procedure(oagitLayoutRestoreCreateShapes(cv records)
  let((record shapeType layer purpose lpp pStyle width sRes shape netName net)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        shapeType = oagitLayoutRestoreString(
                      if(record->type record->type record->shape_kind))
        layer = oagitLayoutRestoreString(record->layer)
        purpose = if(record->purpose oagitLayoutRestoreString(record->purpose) "drawing")
        lpp = list(layer purpose)
        shape = nil
        cond(
          (equal(shapeType "rect")
            sRes = errset(dbCreateRect(cv lpp record->bbox) t)
            shape = if(sRes car(sRes) nil))
          (equal(shapeType "polygon")
            sRes = errset(dbCreatePolygon(cv lpp record->points) t)
            shape = if(sRes car(sRes) nil))
          (equal(shapeType "path")
            pStyle = if(record->path_style
                         oagitLayoutRestoreString(record->path_style)
                         "truncateExtend")
            width = oagitLayoutRestoreFloat(record->width 0.0)
            sRes = errset(dbCreatePath(cv lpp record->points width pStyle) t)
            shape = if(sRes car(sRes) nil)
            when(shape && equal(pStyle "varExtendExtend")
              when(numberp(record->begin_ext)
                errset(shape~>beginExt = float(record->begin_ext) t))
              when(numberp(record->end_ext)
                errset(shape~>endExt = float(record->end_ext) t))))
          (equal(shapeType "path_seg")
            shape = oagitLayoutRestoreCreatePathSeg(cv lpp record))
          (equal(shapeType "ellipse")
            sRes = errset(dbCreateEllipse(cv lpp record->bbox) t)
            shape = if(sRes car(sRes) nil))
          (equal(shapeType "line")
            sRes = errset(dbCreateLine(cv lpp record->points) t)
            shape = if(sRes car(sRes) nil))
          (t
            oagitLayoutRestoreSetFailure(
              "unsupported_object"
              sprintf(nil "unsupported layout shape type %s" shapeType)))
        )
        unless(oagitLayoutRestoreErrorCode
          unless(shape
            oagitLayoutRestoreSetFailure(
              "shape_create_failed"
              sprintf(nil "failed to create %s on %s:%s" shapeType layer purpose)))
          when(shape && !oagitLayoutRestoreErrorCode
            oagitLayoutRestoreCountShapes = oagitLayoutRestoreCountShapes + 1
            netName = if(record->net && strlen(oagitLayoutRestoreString(record->net)) > 0
                          oagitLayoutRestoreString(record->net)
                          nil)
            when(netName
              net = dbMakeNet(cv netName)
              unless(net && dbAddFigToNet(shape net)
                oagitLayoutRestoreSetFailure(
                  "connectivity_create_failed"
                  sprintf(nil "dbAddFigToNet failed for shape net %s" netName))))
            unless(oagitLayoutRestoreErrorCode
              oagitLayoutRestoreApplyProps(shape record->props))
          )
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreFindPinByName(term pinName)
  let((found target)
    found = nil
    target = oagitLayoutRestoreString(pinName)
    when(term && strlen(target) > 0
      foreach(p term~>pins
        when(equal(oagitLayoutRestoreString(p~>name) target)
          found = p)
      )
    )
    found
  )
)

procedure(oagitLayoutRestoreCreatePins(cv records)
  let((record layer purpose lpp shapeKind fRes fig termName pinNet direction
       net term pinNameArg existingPin pRes pin accessDirs)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        layer = oagitLayoutRestoreString(record->layer)
        purpose = if(record->purpose oagitLayoutRestoreString(record->purpose) "pin")
        lpp = list(layer purpose)
        shapeKind = if(record->shape_kind
                        oagitLayoutRestoreString(record->shape_kind)
                        "rect")
        fig = nil
        cond(
          (equal(shapeKind "rect")
            fRes = errset(dbCreateRect(cv lpp record->bbox) t)
            fig = if(fRes car(fRes) nil))
          (equal(shapeKind "polygon")
            fRes = errset(dbCreatePolygon(cv lpp record->points) t)
            fig = if(fRes car(fRes) nil))
          (equal(shapeKind "ellipse")
            fRes = errset(dbCreateEllipse(cv lpp record->bbox) t)
            fig = if(fRes car(fRes) nil))
          (equal(shapeKind "path_seg")
            fig = oagitLayoutRestoreCreatePathSeg(cv lpp record))
          (equal(shapeKind "path")
            let((pStyle width)
              pStyle = if(record->path_style
                           oagitLayoutRestoreString(record->path_style)
                           "truncateExtend")
              width = oagitLayoutRestoreFloat(record->width 0.0)
              fRes = errset(dbCreatePath(cv lpp record->points width pStyle) t)
              fig = if(fRes car(fRes) nil)
              when(fig && equal(pStyle "varExtendExtend")
                when(numberp(record->begin_ext)
                  errset(fig~>beginExt = float(record->begin_ext) t))
                when(numberp(record->end_ext)
                  errset(fig~>endExt = float(record->end_ext) t)))))
          (t
            oagitLayoutRestoreSetFailure(
              "unsupported_object"
              sprintf(nil "unsupported pin shape kind %s" shapeKind)))
        )
        unless(oagitLayoutRestoreErrorCode
          unless(fig
            oagitLayoutRestoreSetFailure(
              "pin_create_failed"
              sprintf(nil "failed to create pin figure for %s"
                      oagitLayoutRestoreString(record->name))))
          when(fig && !oagitLayoutRestoreErrorCode
            termName = oagitLayoutRestoreString(record->name)
            pinNet = if(record->net && strlen(oagitLayoutRestoreString(record->net)) > 0
                         oagitLayoutRestoreString(record->net)
                         termName)
            direction = if(record->direction &&
                           strlen(oagitLayoutRestoreString(record->direction)) > 0
                            oagitLayoutRestoreString(record->direction)
                            "inputOutput")
            net = dbMakeNet(cv pinNet)
            unless(net
              oagitLayoutRestoreSetFailure(
                "pin_create_failed"
                sprintf(nil "dbMakeNet failed for pin net %s" pinNet)))
            when(net && !oagitLayoutRestoreErrorCode
              term = dbFindTermByName(cv termName)
              unless(term
                term = dbCreateTerm(net termName direction))
              unless(term && term~>net == net
                oagitLayoutRestoreSetFailure(
                  "pin_create_failed"
                  sprintf(nil "pin terminal %s is bound to a different net" termName)))
              when(term && !oagitLayoutRestoreErrorCode
                pinNameArg = if(record->pin_name &&
                                strlen(oagitLayoutRestoreString(record->pin_name)) > 0
                                 oagitLayoutRestoreString(record->pin_name)
                                 nil)
                existingPin = if(pinNameArg
                                  oagitLayoutRestoreFindPinByName(term pinNameArg)
                                  nil)
                if(existingPin
                    then
                      pRes = errset(dbAddFigToPin(existingPin fig) t)
                      pin = if(pRes && car(pRes) existingPin nil)
                    else
                      pRes = errset(dbCreatePin(net fig pinNameArg term) t)
                      pin = if(pRes car(pRes) nil))
                unless(pin
                  oagitLayoutRestoreSetFailure(
                    "pin_create_failed"
                    sprintf(nil "dbCreatePin/dbAddFigToPin returned nil for %s" termName)))
                when(pin && !oagitLayoutRestoreErrorCode
                  oagitLayoutRestoreCountPins = oagitLayoutRestoreCountPins + 1
                  when(record->access_dir
                    accessDirs = oagitLayoutRestoreStringList(record->access_dir)
                    when(accessDirs
                      errset(pin~>accessDir = accessDirs t)))
                  oagitLayoutRestoreApplyProps(fig record->props)
                )
              )
            )
          )
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateLabels(cv records)
  let((record layer purpose lpp xy text justify orient font height lRes lbl
       labelType netName net)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        layer = oagitLayoutRestoreString(record->layer)
        purpose = if(record->purpose oagitLayoutRestoreString(record->purpose) "drawing")
        lpp = list(layer purpose)
        xy = if(record->xy record->xy list(0.0 0.0))
        text = oagitLayoutRestoreString(record->text)
        justify = if(record->justify oagitLayoutRestoreString(record->justify) "centerCenter")
        orient = if(record->orient oagitLayoutRestoreString(record->orient) "R0")
        font = if(record->font oagitLayoutRestoreString(record->font) "stick")
        height = oagitLayoutRestoreFloat(record->height 0.1)
        lRes = errset(
          dbCreateLabel(cv lpp xy text justify orient font height)
          t)
        lbl = if(lRes car(lRes) nil)
        unless(lbl
          oagitLayoutRestoreSetFailure(
            "label_create_failed"
            sprintf(nil "dbCreateLabel failed for %s" text)))
        when(lbl && !oagitLayoutRestoreErrorCode
          oagitLayoutRestoreCountLabels = oagitLayoutRestoreCountLabels + 1
          labelType = if(record->label_type
                          oagitLayoutRestoreString(record->label_type)
                          "normalLabel")
          when(equal(labelType "NLPLabel") || equal(labelType "ILLabel")
            errset(lbl~>labelType = labelType t))
          netName = if(record->net && strlen(oagitLayoutRestoreString(record->net)) > 0
                        oagitLayoutRestoreString(record->net)
                        nil)
          when(netName
            net = dbMakeNet(cv netName)
            unless(net && dbAddFigToNet(lbl net)
              oagitLayoutRestoreSetFailure(
                "connectivity_create_failed"
                sprintf(nil "dbAddFigToNet failed for label net %s" netName))))
          unless(oagitLayoutRestoreErrorCode
            oagitLayoutRestoreApplyProps(lbl record->props))
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateBlockages(cv records)
  let((record blkType blkLayer bRes blk)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        blkType = if(record->blockage_type
                      oagitLayoutRestoreString(record->blockage_type)
                      "routing")
        blkLayer = if(record->layer && strlen(oagitLayoutRestoreString(record->layer)) > 0
                       oagitLayoutRestoreString(record->layer)
                       nil)
        bRes = if(blkLayer
                   errset(dbCreateLayerBlockage(cv blkLayer blkType record->points) t)
                   errset(dbCreateAreaBlockage(cv record->points) t))
        blk = if(bRes car(bRes) nil)
        unless(blk
          oagitLayoutRestoreSetFailure(
            "blockage_create_failed"
            sprintf(nil "failed to create %s blockage" blkType)))
        when(blk && !oagitLayoutRestoreErrorCode
          oagitLayoutRestoreCountBlockages = oagitLayoutRestoreCountBlockages + 1
          oagitLayoutRestoreApplyProps(blk record->props)
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreCreateFigGroups(cv records)
  let((record fgName origin orient groupType fgRes fg)
    foreach(record records
      unless(oagitLayoutRestoreErrorCode
        fgName = oagitLayoutRestoreString(record->name)
        origin = if(record->origin record->origin list(0.0 0.0))
        orient = if(record->orient oagitLayoutRestoreString(record->orient) "R0")
        groupType = if(record->group_type oagitLayoutRestoreString(record->group_type) "none")
        fgRes = errset(dbCreateFigGroup(cv fgName t origin orient) t)
        fg = if(fgRes car(fgRes) nil)
        unless(fg
          oagitLayoutRestoreSetFailure(
            "fig_group_create_failed"
            sprintf(nil "dbCreateFigGroup failed for %s" fgName)))
        when(fg && !oagitLayoutRestoreErrorCode
          when(strlen(groupType) > 0 && !equal(groupType "none") && !equal(groupType "default")
            errset(fg~>type = groupType t))
          oagitLayoutRestoreCountFigGroups = oagitLayoutRestoreCountFigGroups + 1
          oagitLayoutRestoreApplyProps(fg record->props)
        )
      )
    )
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreFindProp(props name)
  let((found target)
    found = nil
    target = oagitLayoutRestoreString(name)
    foreach(prop props
      when(equal(oagitLayoutRestoreString(prop~>name) target)
        found = prop)
    )
    found
  )
)

procedure(oagitLayoutRestoreValuesEquivP(actual expected paramType)
  let((aNum eNum diff denom)
    cond(
      (equal(paramType "boolean")
        eq(oagitLayoutRestoreBoolNorm(actual)
           oagitLayoutRestoreBoolNorm(expected)))
      (equal(actual expected) t)
      (t
        aNum = cond(
          (numberp(actual) float(actual))
          (stringp(actual)
            let((r) r = errset(cdfParseFloatString(actual) t)
              if(r && numberp(car(r)) float(car(r)) nil)))
          (t nil))
        eNum = cond(
          (numberp(expected) float(expected))
          (stringp(expected)
            let((r) r = errset(cdfParseFloatString(expected) t)
              if(r && numberp(car(r)) float(car(r)) nil)))
          (t nil))
        if(aNum && eNum
            then
              diff = abs(aNum - eNum)
              denom = max(abs(aNum) abs(eNum) 1.0e-18)
              diff <= 1.0e-9 * denom
            else nil))
    )
  )
)

procedure(oagitLayoutRestorePCellMismatch(instName paramName expected actual paramType reason)
  list('jsonObject
       list("instance" oagitLayoutRestoreString(instName))
       list("param" oagitLayoutRestoreString(paramName))
       list("expected" oagitLayoutRestoreJsonVal(expected paramType))
       list("actual" if(actual != nil || equal(paramType "boolean")
                         oagitLayoutRestoreJsonVal(actual paramType)
                         list('jsonNull)))
       list("reason" reason))
)

procedure(oagitLayoutRestoreFindEffectivePCellParam(obj name)
  let((prop master paramRes superMaster)
    prop = if(obj oagitLayoutRestoreFindProp(obj~>prop name) nil)
    unless(prop
      when(obj
        master = obj~>master
        when(master
          paramRes = errset(master~>parameters~>value t)
          when(paramRes && car(paramRes) && listp(car(paramRes))
            prop = oagitLayoutRestoreFindProp(car(paramRes) name))
          unless(prop
            superMaster = master~>superMaster
            when(superMaster
              paramRes = errset(superMaster~>parameters~>value t)
              when(paramRes && car(paramRes) && listp(car(paramRes))
                prop = oagitLayoutRestoreFindProp(car(paramRes) name))))
        )
      )
    )
    prop
  )
)

procedure(oagitLayoutRestoreVerifyPCellOnObj(instName obj pcellParams)
  let((triple pName pRawVal rawType dbType expectedVal prop actualVal)
    foreach(triple pcellParams
      pName = oagitLayoutRestoreString(car(triple))
      pRawVal = cadr(triple)
      rawType = if(length(triple) >= 3
                    oagitLayoutRestoreString(caddr(triple))
                    oagitLayoutRestoreInferType(pRawVal))
      dbType = oagitLayoutRestoreDbParamType(rawType)
      expectedVal = oagitLayoutRestoreParamValue(pRawVal dbType)
      prop = oagitLayoutRestoreFindEffectivePCellParam(obj pName)
      if(!prop
          then
            oagitLayoutRestorePCellMismatches = cons(
              oagitLayoutRestorePCellMismatch(
                instName pName expectedVal nil dbType "missing_pcell_param")
              oagitLayoutRestorePCellMismatches)
          else
            actualVal = prop~>value
            unless(oagitLayoutRestoreValuesEquivP(actualVal expectedVal dbType)
              oagitLayoutRestorePCellMismatches = cons(
                oagitLayoutRestorePCellMismatch(
                  instName pName expectedVal actualVal dbType "value_mismatch")
                oagitLayoutRestorePCellMismatches)))
    )
  )
)

procedure(oagitLayoutRestoreVerifyPCells(instRecords instByName mosaicRecords mosaicByName)
  let((record instName inst mosaic tplInst targetObj)
    oagitLayoutRestorePCellMismatches = nil
    foreach(record instRecords
      when(record->is_pcell && record->pcell_params
        instName = oagitLayoutRestoreString(record->name)
        inst = instByName[instName]
        oagitLayoutRestoreVerifyPCellOnObj(instName inst record->pcell_params)
      )
    )
    foreach(record mosaicRecords
      when(record->is_pcell && record->pcell_params
        instName = oagitLayoutRestoreString(record->name)
        mosaic = mosaicByName[instName]
        tplInst = if(mosaic && listp(mosaic~>instanceList) car(mosaic~>instanceList) nil)
        targetObj = if(tplInst tplInst mosaic)
        oagitLayoutRestoreVerifyPCellOnObj(instName targetObj record->pcell_params)
      )
    )
    oagitLayoutRestorePCellMismatches = reverse(oagitLayoutRestorePCellMismatches)
    if(oagitLayoutRestorePCellMismatches
        then
          oagitLayoutRestoreSetFailure(
            "pcell_param_mismatch"
            "PCell parameter readback mismatch in draft CellView")
        else
          oagitLayoutRestorePCellVerified = t
          t)
  )
)

procedure(oagitLayoutRestoreFindInstTerm(inst name)
  let((found target)
    found = nil
    target = oagitLayoutRestoreString(name)
    when(inst
      foreach(instTerm inst~>instTerms
        when(equal(oagitLayoutRestoreString(instTerm~>name) target)
          found = instTerm)
      )
    )
    found
  )
)

procedure(oagitLayoutRestoreConnMismatch(instName termName expected actual reason)
  list('jsonObject
       list("instance" oagitLayoutRestoreString(instName))
       list("terminal" oagitLayoutRestoreString(termName))
       list("expected" oagitLayoutRestoreMaybeString(expected))
       list("actual" oagitLayoutRestoreMaybeString(actual))
       list("reason" reason))
)

procedure(oagitLayoutRestoreVerifyConnectivity(cv instRecords instByName pinRecords)
  let((record instName inst pair expectedTerm expectedNet instTerm actualNet
       pinRec termName expectedDir term actualDir)
    oagitLayoutRestoreConnMismatches = nil
    foreach(record instRecords
      instName = oagitLayoutRestoreString(record->name)
      inst = instByName[instName]
      foreach(pair record->connections
        expectedTerm = oagitLayoutRestoreString(car(pair))
        expectedNet = oagitLayoutRestoreString(cadr(pair))
        instTerm = oagitLayoutRestoreFindInstTerm(inst expectedTerm)
        if(instTerm
            then
              actualNet = if(instTerm~>net instTerm~>net~>name nil)
              unless(equal(expectedNet oagitLayoutRestoreString(actualNet))
                oagitLayoutRestoreConnMismatches = cons(
                  oagitLayoutRestoreConnMismatch(
                    instName expectedTerm expectedNet actualNet "net_mismatch")
                  oagitLayoutRestoreConnMismatches))
            else
              oagitLayoutRestoreConnMismatches = cons(
                oagitLayoutRestoreConnMismatch(
                  instName expectedTerm expectedNet nil "missing_terminal")
                oagitLayoutRestoreConnMismatches))
      )
    )
    foreach(pinRec pinRecords
      termName = oagitLayoutRestoreString(pinRec->name)
      expectedNet = if(pinRec->net && strlen(oagitLayoutRestoreString(pinRec->net)) > 0
                        oagitLayoutRestoreString(pinRec->net)
                        termName)
      expectedDir = if(pinRec->direction &&
                       strlen(oagitLayoutRestoreString(pinRec->direction)) > 0
                        oagitLayoutRestoreString(pinRec->direction)
                        "inputOutput")
      term = dbFindTermByName(cv termName)
      if(!term
          then
            oagitLayoutRestoreConnMismatches = cons(
              oagitLayoutRestoreConnMismatch(
                "<pin>" termName expectedNet nil "missing_pin_terminal")
              oagitLayoutRestoreConnMismatches)
          else
            actualNet = if(term~>net term~>net~>name nil)
            actualDir = if(term~>direction oagitLayoutRestoreString(term~>direction) "")
            unless(equal(expectedNet oagitLayoutRestoreString(actualNet))
              oagitLayoutRestoreConnMismatches = cons(
                oagitLayoutRestoreConnMismatch(
                  "<pin>" termName expectedNet actualNet "pin_net_mismatch")
                oagitLayoutRestoreConnMismatches))
            unless(equal(expectedDir actualDir)
              oagitLayoutRestoreConnMismatches = cons(
                oagitLayoutRestoreConnMismatch(
                  "<pin>" termName expectedDir actualDir "pin_direction_mismatch")
                oagitLayoutRestoreConnMismatches)))
    )
    oagitLayoutRestoreConnMismatches = reverse(oagitLayoutRestoreConnMismatches)
    if(oagitLayoutRestoreConnMismatches
        then
          oagitLayoutRestoreSetFailure(
            "connectivity_mismatch"
            "layout connectivity verification mismatch in draft CellView")
        else
          oagitLayoutRestoreConnVerified = t
          t)
  )
)

procedure(oagitLayoutRestoreCountNonDisplayShapes(cv)
  let((count shapesRes)
    count = 0
    shapesRes = errset(cv~>shapes t)
    when(shapesRes && car(shapesRes)
      foreach(shape car(shapesRes)
        unless(equal(oagitLayoutRestoreString(shape~>objType) "textDisplay") &&
               (shape~>parent || shape~>associate || shape~>owner)
          count = count + 1)
      )
    )
    count
  )
)

procedure(oagitLayoutRestoreCountNonMosaicInstances(cv)
  let((count instsRes objType)
    count = 0
    instsRes = errset(cv~>instances t)
    when(instsRes && car(instsRes)
      foreach(inst car(instsRes)
        objType = oagitLayoutRestoreString(inst~>objType)
        unless(inst~>mosaic || equal(objType "mosaicInst") || equal(objType "mosaic")
          count = count + 1)
      )
    )
    count
  )
)

procedure(oagitLayoutRestoreVerifyCounts(cv req)
  let((expectedVias actualViasRes actualVias expectedInsts actualInsts
       expectedMosaics actualMosaicsRes actualMosaics expectedShapes
       actualShapes)
    expectedVias = length(req->vias)
    actualViasRes = errset(cv~>vias t)
    actualVias = if(actualViasRes && listp(car(actualViasRes))
                     length(car(actualViasRes))
                     0)
    expectedInsts = length(req->instances)
    actualInsts = oagitLayoutRestoreCountNonMosaicInstances(cv)
    expectedMosaics = length(req->mosaics)
    actualMosaicsRes = errset(cv~>mosaics t)
    actualMosaics = if(actualMosaicsRes && listp(car(actualMosaicsRes))
                        length(car(actualMosaicsRes))
                        0)
    expectedShapes = length(req->shapes) + length(req->pins) + length(req->labels)
    actualShapes = oagitLayoutRestoreCountNonDisplayShapes(cv)
    cond(
      (oagitLayoutRestoreCountVias != expectedVias || actualVias != expectedVias
        oagitLayoutRestoreSetFailure(
          "object_count_mismatch"
          sprintf(nil "via count mismatch: expected=%d created=%d in_cv=%d"
                  expectedVias oagitLayoutRestoreCountVias actualVias)))
      (oagitLayoutRestoreCountInstances != expectedInsts || actualInsts != expectedInsts
        oagitLayoutRestoreSetFailure(
          "object_count_mismatch"
          sprintf(nil "instance count mismatch: expected=%d created=%d in_cv=%d"
                  expectedInsts oagitLayoutRestoreCountInstances actualInsts)))
      (oagitLayoutRestoreCountMosaics != expectedMosaics || actualMosaics != expectedMosaics
        oagitLayoutRestoreSetFailure(
          "object_count_mismatch"
          sprintf(nil "mosaic count mismatch: expected=%d created=%d in_cv=%d"
                  expectedMosaics oagitLayoutRestoreCountMosaics actualMosaics)))
      (oagitLayoutRestoreCountShapes != length(req->shapes) ||
       oagitLayoutRestoreCountPins != length(req->pins) ||
       oagitLayoutRestoreCountLabels != length(req->labels) ||
       actualShapes != expectedShapes
        oagitLayoutRestoreSetFailure(
          "object_count_mismatch"
          sprintf(nil "shape/pin/label count mismatch: expected=%d in_cv=%d"
                  expectedShapes actualShapes)))
      (t t)
    )
  )
)

procedure(oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
  let((purgeResult draftDd deleteResult cleanupOk)
    cleanupOk = t
    when(cv
      purgeResult = errset(dbPurge(cv) t)
      unless(purgeResult && car(purgeResult)
        cleanupOk = nil
        oagitLayoutRestoreSetFailure(
          "cleanup_failed"
          "dbPurge failed during layout draft cleanup"))
    )
    draftDd = ddGetObj(lib draft_cell)
    when(draftDd
      deleteResult = errset(ddDeleteObj(draftDd) t)
      unless(deleteResult && car(deleteResult)
        cleanupOk = nil
        oagitLayoutRestoreSetFailure(
          "cleanup_failed"
          "ddDeleteObj failed during layout draft cleanup"))
    )
    cleanupOk
  )
)

procedure(oagitLayoutRestoreBuild(req cv techFile instByName mosaicByName)
  let((saveResult)
    oagitLayoutRestoreCreateNets(cv req->nets)
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateBoundaries(cv req->boundaries))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateInstances(cv req->instances instByName))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateMosaics(cv req->mosaics mosaicByName))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateVias(cv techFile req->vias))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateShapes(cv req->shapes))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreatePins(cv req->pins))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateLabels(cv req->labels))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateBlockages(cv req->blockages))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreCreateFigGroups(cv req->fig_groups))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreApplyProps(cv req->properties))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreVerifyPCells(
        req->instances instByName req->mosaics mosaicByName))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreVerifyConnectivity(
        cv req->instances instByName req->pins))
    unless(oagitLayoutRestoreErrorCode
      oagitLayoutRestoreVerifyCounts(cv req))
    unless(oagitLayoutRestoreErrorCode
      saveResult = errset(dbSave(cv) t)
      unless(saveResult && car(saveResult)
        oagitLayoutRestoreSetFailure("save_failed" "dbSave failed")))
    !oagitLayoutRestoreErrorCode
  )
)

procedure(oagitLayoutRestoreRun(req)
  let((target lib cell view draft_cell overwrite cv tfRes techFile
       instByName mosaicByName buildResult copyResult pubCv stillOpen cleanupResult)
    oagitLayoutRestoreErrorCode = nil
    oagitLayoutRestoreErrorMessage = nil
    oagitLayoutRestorePCellVerified = nil
    oagitLayoutRestoreConnVerified = nil
    oagitLayoutRestorePCellMismatches = nil
    oagitLayoutRestoreConnMismatches = nil
    oagitLayoutRestoreCountNets = 0
    oagitLayoutRestoreCountBoundaries = 0
    oagitLayoutRestoreCountInstances = 0
    oagitLayoutRestoreCountMosaics = 0
    oagitLayoutRestoreCountVias = 0
    oagitLayoutRestoreCountShapes = 0
    oagitLayoutRestoreCountPins = 0
    oagitLayoutRestoreCountLabels = 0
    oagitLayoutRestoreCountBlockages = 0
    oagitLayoutRestoreCountFigGroups = 0
    target = req->target
    lib = target->library
    cell = target->cell
    view = target->view
    draft_cell = req->draft_cell
    overwrite = req->overwrite
    cond(
      (!member(lib req->allow_libraries)
        oagitLayoutRestoreFail(req "disallowed_library"
                               "target library is not allow-listed"))
      (oagitDirHasLiveLocks(lib cell view)
        oagitLayoutRestoreFail(req "live_lock"
                               "target view has a live edit lock"))
      (ddGetObj(lib cell view) && !overwrite
        oagitLayoutRestoreFail(req "target_exists"
                               "target CellView already exists"))
      (equal(draft_cell cell)
        oagitLayoutRestoreFail(req "draft_exists"
                               "draft cell must be distinct from target cell"))
      (ddGetObj(lib draft_cell)
        oagitLayoutRestoreFail(req "draft_exists"
                               "draft cell already exists"))
      (t
        cv = dbOpenCellViewByType(lib draft_cell view "maskLayout" "w")
        unless(cv
          oagitLayoutRestoreFail(req "draft_open_failed"
                                 "unable to open draft layout CellView"))
        when(cv
          tfRes = errset(techGetTechFile(cv) t)
          techFile = if(tfRes car(tfRes) nil)
          instByName = makeTable("oagitLayoutRestoreInstances" nil)
          mosaicByName = makeTable("oagitLayoutRestoreMosaics" nil)
          buildResult = errset(
            oagitLayoutRestoreBuild(req cv techFile instByName mosaicByName)
            t)
          unless(buildResult
            oagitLayoutRestoreSetFailure(
              "restore_failed"
              sprintf(nil "unexpected layout restore error: %L" errset.errset)))
          if(oagitLayoutRestoreErrorCode
              then
                oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
                oagitLayoutRestoreWriteResponse(req nil nil)
              else
                copyResult = errset(
                  dbCopyCellView(cv lib cell view nil nil t)
                  t)
                pubCv = nil
                unless(copyResult && car(copyResult)
                  oagitLayoutRestoreSetFailure(
                    "publish_failed"
                    "dbCopyCellView failed"))
                if(oagitLayoutRestoreErrorCode
                    then
                      oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
                      oagitLayoutRestoreWriteResponse(req nil nil)
                    else
                      pubCv = car(copyResult)
                      cleanupResult = oagitLayoutRestoreCleanupDraft(
                        lib draft_cell view cv)
                      unless(cleanupResult
                        oagitLayoutRestoreSetFailure(
                          "cleanup_failed"
                          "layout draft cleanup failed"))
                      when(pubCv && !eq(pubCv cv)
                        errset(dbClose(pubCv) t)
                        errset(dbPurge(pubCv) t))
                      stillOpen = dbFindOpenCellViewByName(lib cell view)
                      when(stillOpen && !eq(stillOpen cv)
                        errset(dbClose(stillOpen) t)
                        errset(dbPurge(stillOpen) t))
                      if(oagitLayoutRestoreErrorCode
                          then oagitLayoutRestoreWriteResponse(req nil nil)
                          else oagitLayoutRestoreWriteResponse(req t t)))
          )
        ))
    )
  )
)

procedure(oagitLayoutRunRestore(req)
  /* Public Track 1 daemon.il entrypoint alias for restore_layout. */
  oagitLayoutRestoreRun(req)
)

procedure(oagitLayoutRestoreClosePublished(pubCv draftCv lib cell view)
  let((stillOpen)
    when(pubCv && !eq(pubCv draftCv)
      errset(dbClose(pubCv) t)
      errset(dbPurge(pubCv) t))
    stillOpen = dbFindOpenCellViewByName(lib cell view)
    when(stillOpen && !eq(stillOpen draftCv)
      errset(dbClose(stillOpen) t)
      errset(dbPurge(stillOpen) t))
    t
  )
)

procedure(oagitLayoutRestoreReadback(lib cell view)
  let((rbCv openedByScript capRes)
    cond(
      (!isCallable('oagitLayoutExportBuildResponse)
        list(nil "readback_unavailable" "oagitLayoutExportBuildResponse is not loaded"))
      (t
        rbCv = dbFindOpenCellViewByName(lib cell view)
        if(rbCv
            then openedByScript = nil
            else
              rbCv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        if(!rbCv
            then list(nil "readback_open_failed" "unable to open published layout for readback")
            else
              capRes = errset(oagitLayoutExportBuildResponse(rbCv lib cell view) t)
              when(openedByScript errset(dbClose(rbCv) t))
              if(capRes && car(capRes)
                  then list(t car(capRes) nil)
                  else list(nil "readback_failed"
                            sprintf(nil "same-session layout readback failed: %L" errset.errset)))))
    )
  )
)

procedure(oagitLayoutRestoreItemResult(lib cell view ok published draftCell readbackObj)
  list('jsonObject
       list("target"
            list('jsonObject
                 list("library" oagitLayoutRestoreString(lib))
                 list("cell" oagitLayoutRestoreString(cell))
                 list("view" oagitLayoutRestoreString(view))))
       list("ok" oagitLayoutRestoreBool(ok))
       list("published" oagitLayoutRestoreBool(published))
       list("draft_cell" if(draftCell oagitLayoutRestoreString(draftCell) list('jsonNull)))
       list("pcell_verified" oagitLayoutRestoreBool(oagitLayoutRestorePCellVerified))
       list("connectivity_verified" oagitLayoutRestoreBool(oagitLayoutRestoreConnVerified))
       list("object_counts" oagitLayoutRestoreCountsObject())
       list("pcell_mismatches" cons('jsonArray oagitLayoutRestorePCellMismatches))
       list("connectivity_mismatches" cons('jsonArray oagitLayoutRestoreConnMismatches))
       list("readback" if(readbackObj readbackObj list('jsonNull)))
       list("error_code" if(ok list('jsonNull)
                                if(oagitLayoutRestoreErrorCode
                                    oagitLayoutRestoreErrorCode
                                    list('jsonNull))))
       list("error" if(ok list('jsonNull)
                           if(oagitLayoutRestoreErrorCode
                               oagitLayoutRestoreErrorMessage
                               list('jsonNull)))))
)

procedure(oagitLayoutRestoreItem(item doReadback)
  let((target lib cell view draft_cell overwrite allowLibs
       cv tfRes techFile instByName mosaicByName buildResult
       copyResult pubCv cleanupResult rbRes)
    oagitLayoutRestoreErrorCode = nil
    oagitLayoutRestoreErrorMessage = nil
    oagitLayoutRestorePCellVerified = nil
    oagitLayoutRestoreConnVerified = nil
    oagitLayoutRestorePCellMismatches = nil
    oagitLayoutRestoreConnMismatches = nil
    oagitLayoutRestoreCountNets = 0
    oagitLayoutRestoreCountBoundaries = 0
    oagitLayoutRestoreCountInstances = 0
    oagitLayoutRestoreCountMosaics = 0
    oagitLayoutRestoreCountVias = 0
    oagitLayoutRestoreCountShapes = 0
    oagitLayoutRestoreCountPins = 0
    oagitLayoutRestoreCountLabels = 0
    oagitLayoutRestoreCountBlockages = 0
    oagitLayoutRestoreCountFigGroups = 0
    target = item->target
    lib = oagitLayoutRestoreString(target->library)
    cell = oagitLayoutRestoreString(target->cell)
    view = oagitLayoutRestoreString(target->view)
    draft_cell = oagitLayoutRestoreString(item->draft_cell)
    overwrite = item->overwrite
    allowLibs = item->allow_libraries
    cond(
      (!member(lib allowLibs)
        oagitLayoutRestoreSetFailure("disallowed_library" "target library is not allow-listed")
        list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil)))
      (oagitDirHasLiveLocks(lib cell view)
        oagitLayoutRestoreSetFailure("live_lock" "target view has a live edit lock")
        list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil)))
      (ddGetObj(lib cell view) && !overwrite
        oagitLayoutRestoreSetFailure("target_exists" "target CellView already exists")
        list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil)))
      (equal(draft_cell cell)
        oagitLayoutRestoreSetFailure("draft_exists" "draft cell must be distinct from target cell")
        list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil)))
      (ddGetObj(lib draft_cell)
        oagitLayoutRestoreSetFailure("draft_exists" "draft cell already exists")
        list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil)))
      (t
        cv = dbOpenCellViewByType(lib draft_cell view "maskLayout" "w")
        if(!cv
            then
              oagitLayoutRestoreSetFailure("draft_open_failed" "unable to open draft layout CellView")
              list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil))
            else
              tfRes = errset(techGetTechFile(cv) t)
              techFile = if(tfRes car(tfRes) nil)
              instByName = makeTable("oagitLayoutRestoreItemInstances" nil)
              mosaicByName = makeTable("oagitLayoutRestoreItemMosaics" nil)
              buildResult = errset(
                oagitLayoutRestoreBuild(item cv techFile instByName mosaicByName)
                t)
              unless(buildResult
                oagitLayoutRestoreSetFailure(
                  "restore_failed"
                  sprintf(nil "unexpected layout restore error: %L" errset.errset)))
              if(oagitLayoutRestoreErrorCode
                  then
                    oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
                    list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil))
                  else
                    copyResult = errset(
                      dbCopyCellView(cv lib cell view nil nil t)
                      t)
                    pubCv = nil
                    unless(copyResult && car(copyResult)
                      oagitLayoutRestoreSetFailure("publish_failed" "dbCopyCellView failed"))
                    if(oagitLayoutRestoreErrorCode
                        then
                          oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
                          list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil))
                        else
                          pubCv = car(copyResult)
                          cleanupResult = oagitLayoutRestoreCleanupDraft(lib draft_cell view cv)
                          unless(cleanupResult
                            oagitLayoutRestoreSetFailure("cleanup_failed" "layout draft cleanup failed"))
                          if(oagitLayoutRestoreErrorCode
                              then
                                oagitLayoutRestoreClosePublished(pubCv cv lib cell view)
                                list(nil oagitLayoutRestoreItemResult(lib cell view nil nil draft_cell nil))
                              else
                                if(doReadback
                                    then
                                      rbRes = oagitLayoutRestoreReadback(lib cell view)
                                      oagitLayoutRestoreClosePublished(pubCv cv lib cell view)
                                      if(car(rbRes)
                                          then
                                            list(t oagitLayoutRestoreItemResult(lib cell view t t draft_cell cadr(rbRes)))
                                          else
                                            oagitLayoutRestoreSetFailure(cadr(rbRes) caddr(rbRes))
                                            list(nil oagitLayoutRestoreItemResult(lib cell view nil t draft_cell nil)))
                                    else
                                      oagitLayoutRestoreClosePublished(pubCv cv lib cell view)
                                      list(t oagitLayoutRestoreItemResult(lib cell view t t draft_cell nil))))))))
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
     oagitRequest->action == "restore_layout"
  oagitLayoutRestoreRun(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
