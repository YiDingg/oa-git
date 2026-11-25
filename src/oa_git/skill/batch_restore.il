/* Single-session batch restore with same-session readback for oa-git (oagit-batch/1).
 *
 * Performs atomic draft-and-publish restore per target item in one Virtuoso
 * session. When options->readback is non-nil, immediately captures each
 * published target in the same session and returns the inline readback payload,
 * eliminating the second Virtuoso cold start.
 * All functions are prefixed with oagitBatch*.
 */

procedure(oagitBatchResString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitBatchResBool(value)
  list('jsonBool if(value t nil))
)

procedure(oagitBatchResTargetObj(lib cell view)
  list('jsonObject
       list("library" oagitBatchResString(lib))
       list("cell" oagitBatchResString(cell))
       list("view" oagitBatchResString(view)))
)

procedure(oagitBatchResWriteAtomic(outPath payload)
  let((tmpPath written renamed)
    tmpPath = strcat(outPath ".tmp")
    written = errset(oagitWriteJsonFile(tmpPath payload) t)
    if(written && car(written)
        then
          renamed = errset(renameFile(tmpPath outPath) t)
          if(renamed && car(renamed)
              then t
              else
                errset(deleteFile(tmpPath) t)
                nil)
        else
          errset(deleteFile(tmpPath) t)
          nil)
  )
)

procedure(oagitBatchResItemResult(lib cell view ok published draftCell
                                  schErrors schWarnings cdfVerified mismatches
                                  readbackObj errCode errMsg
                                  @optional (extraPairs nil))
  let((pairs)
    pairs = list(
      list("target" oagitBatchResTargetObj(lib cell view))
      list("ok" oagitBatchResBool(ok))
      list("published" oagitBatchResBool(published))
      list("draft_cell" if(draftCell oagitBatchResString(draftCell) list('jsonNull)))
      list("sch_check"
           list('jsonObject
                list("errors" if(integerp(schErrors) schErrors 0))
                list("warnings" if(integerp(schWarnings) schWarnings 0))))
      list("cdf_verified" oagitBatchResBool(cdfVerified))
      list("connectivity_mismatches" cons('jsonArray mismatches))
      list("readback" if(readbackObj readbackObj list('jsonNull)))
      list("error_code" if(errCode oagitBatchResString(errCode) list('jsonNull)))
      list("error" if(errMsg oagitBatchResString(errMsg) list('jsonNull)))
    )
    when(extraPairs
      pairs = append(pairs extraPairs)
    )
    cons('jsonObject pairs)
  )
)

procedure(oagitBatchResClosePublished(pubCv lib cell view)
  let((stillOpen)
    when(pubCv
      errset(dbClose(pubCv) t)
      errset(dbPurge(pubCv) t)
    )
    stillOpen = dbFindOpenCellViewByName(lib cell view)
    when(stillOpen
      errset(dbClose(stillOpen) t)
      errset(dbPurge(stillOpen) t)
    )
    t
  )
)

procedure(oagitBatchResSchTelemetry(item ok)
  let((cdfPasses cbOrder derivedRefresh derivedStatus)
    cdfPasses = if(boundp('oagitRestoreCdfPasses) && integerp(oagitRestoreCdfPasses)
                    oagitRestoreCdfPasses 0)
    cbOrder = if(boundp('oagitRestoreCallbackOrder)
                  oagitRestoreCallbackOrder nil)
    derivedRefresh = if(item && item->recompute_derived
                         then cons('jsonObject
                                   reverse(if(boundp('oagitRestoreDerivedRefresh)
                                               oagitRestoreDerivedRefresh nil)))
                         else list('jsonNull))
    derivedStatus = if(ok && item && item->recompute_derived
                        then "valid"
                        else list('jsonNull))
    list(
      list("cdf_passes" cdfPasses)
      list("callback_order" cons('jsonObject reverse(cbOrder)))
      list("derived_status" derivedStatus)
      list("derived_refresh" derivedRefresh)
    )
  )
)

procedure(oagitBatchResSymTelemetry(pinVerified pinMismatches symErrors symWarnings)
  list(
    list("pin_contract_verified" oagitBatchResBool(pinVerified))
    list("pin_mismatches" cons('jsonArray pinMismatches))
    list("symbol_check"
         list('jsonObject
              list("errors" if(integerp(symErrors) symErrors 0))
              list("warnings" if(integerp(symWarnings) symWarnings 0))))
  )
)

procedure(oagitBatchResReadbackSchematic(lib cell view)
  let((cv openedByScript capRes)
    cond(
      (!isCallable('oagitExportBuildResponse)
        list(nil "readback_unavailable" "oagitExportBuildResponse is not loaded"))
      (t
        cv = dbFindOpenCellViewByName(lib cell view)
        if(cv
            then openedByScript = nil
            else
              cv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        if(!cv
            then list(nil "readback_open_failed" "unable to open published target for readback")
            else
              capRes = errset(oagitExportBuildResponse(cv lib cell view) t)
              when(openedByScript errset(dbClose(cv) t))
              if(capRes && car(capRes)
                  then list(t car(capRes) nil)
                  else list(nil "readback_failed"
                            sprintf(nil "same-session readback failed: %L" errset.errset)))))
    )
  )
)

procedure(oagitBatchResRestoreSchematic(item doReadback)
  let((target lib cell view draftCell overwrite allowLibs
       cv instByName buildResult copyResult pubCv cleanupResult rbRes)
    target = item->target
    lib = oagitBatchResString(target->library)
    cell = oagitBatchResString(target->cell)
    view = oagitBatchResString(target->view)
    draftCell = oagitBatchResString(item->draft_cell)
    overwrite = item->overwrite
    allowLibs = item->allow_libraries

    oagitRestoreErrorCode = nil
    oagitRestoreErrorMessage = nil
    oagitRestoreSchErrors = 0
    oagitRestoreSchWarnings = 0
    oagitRestoreCdfVerified = nil
    oagitRestoreMismatches = nil
    oagitRestoreCdfPasses = 0
    oagitRestoreCallbackOrder = nil
    oagitRestoreDerivedRefresh = nil

    cond(
      (!isCallable('oagitRestoreBuild) || !isCallable('oagitRestoreCleanupDraft)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "restore_unavailable" "restore_schematic.il is not loaded"
            oagitBatchResSchTelemetry(item nil))))
      (!member(lib allowLibs)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "disallowed_library" "target library is not allow-listed"
            oagitBatchResSchTelemetry(item nil))))
      (oagitDirHasLiveLocks(lib cell view)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "live_lock" "target view has a live edit lock"
            oagitBatchResSchTelemetry(item nil))))
      (ddGetObj(lib cell view) && !overwrite
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "target_exists" "target CellView already exists"
            oagitBatchResSchTelemetry(item nil))))
      (equal(draftCell cell)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "draft_exists" "draft cell must be distinct from target cell"
            oagitBatchResSchTelemetry(item nil))))
      (ddGetObj(lib draftCell view)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "draft_exists" "draft CellView already exists"
            oagitBatchResSchTelemetry(item nil))))
      (t
        cv = dbOpenCellViewByType(lib draftCell view "schematic" "w")
        if(!cv
            then
              list(
                nil
                oagitBatchResItemResult(
                  lib cell view nil nil draftCell 0 0 nil nil nil
                  "draft_open_failed" "unable to open draft CellView"
                  oagitBatchResSchTelemetry(item nil)))
            else
              instByName = makeTable("oagitBatchRestoreInstances" nil)
              buildResult = errset(oagitRestoreBuild(item cv instByName) t)
              unless(buildResult
                oagitRestoreSetFailure(
                  "restore_failed"
                  sprintf(nil "unexpected restore error: %L" errset.errset))
              )
              if(oagitRestoreErrorCode
                  then
                    oagitRestoreCleanupDraft(lib draftCell view cv)
                    list(
                      nil
                      oagitBatchResItemResult(
                        lib cell view nil nil draftCell
                        oagitRestoreSchErrors oagitRestoreSchWarnings
                        oagitRestoreCdfVerified oagitRestoreMismatches nil
                        oagitRestoreErrorCode oagitRestoreErrorMessage
                        oagitBatchResSchTelemetry(item nil)))
                  else
                    copyResult = errset(
                      dbCopyCellView(cv lib cell view nil nil t)
                      t)
                    pubCv = nil
                    unless(copyResult && car(copyResult)
                      oagitRestoreSetFailure("publish_failed" "dbCopyCellView failed")
                    )
                    if(oagitRestoreErrorCode
                        then
                          oagitRestoreCleanupDraft(lib draftCell view cv)
                          list(
                            nil
                            oagitBatchResItemResult(
                              lib cell view nil nil draftCell
                              oagitRestoreSchErrors oagitRestoreSchWarnings
                              oagitRestoreCdfVerified oagitRestoreMismatches nil
                              oagitRestoreErrorCode oagitRestoreErrorMessage
                              oagitBatchResSchTelemetry(item nil)))
                        else
                          pubCv = car(copyResult)
                          cleanupResult = oagitRestoreCleanupDraft(lib draftCell view cv)
                          unless(cleanupResult
                            unless(oagitRestoreErrorCode
                              oagitRestoreSetFailure("cleanup_failed" "draft cleanup failed")
                            )
                          )
                          if(oagitRestoreErrorCode
                              then
                                oagitBatchResClosePublished(pubCv lib cell view)
                                list(
                                  nil
                                  oagitBatchResItemResult(
                                    lib cell view nil nil draftCell
                                    oagitRestoreSchErrors oagitRestoreSchWarnings
                                    oagitRestoreCdfVerified oagitRestoreMismatches nil
                                    oagitRestoreErrorCode oagitRestoreErrorMessage
                                    oagitBatchResSchTelemetry(item nil)))
                              else
                                if(doReadback
                                    then
                                      rbRes = oagitBatchResReadbackSchematic(lib cell view)
                                      oagitBatchResClosePublished(pubCv lib cell view)
                                      if(car(rbRes)
                                          then
                                            list(
                                              t
                                              oagitBatchResItemResult(
                                                lib cell view t t draftCell
                                                oagitRestoreSchErrors oagitRestoreSchWarnings
                                                oagitRestoreCdfVerified nil cadr(rbRes)
                                                nil nil
                                                oagitBatchResSchTelemetry(item t)))
                                          else
                                            list(
                                              nil
                                              oagitBatchResItemResult(
                                                lib cell view nil t draftCell
                                                oagitRestoreSchErrors oagitRestoreSchWarnings
                                                oagitRestoreCdfVerified nil nil
                                                cadr(rbRes) caddr(rbRes)
                                                oagitBatchResSchTelemetry(item nil))))
                                    else
                                      oagitBatchResClosePublished(pubCv lib cell view)
                                      list(
                                        t
                                        oagitBatchResItemResult(
                                          lib cell view t t draftCell
                                          oagitRestoreSchErrors oagitRestoreSchWarnings
                                          oagitRestoreCdfVerified nil nil
                                          nil nil
                                          oagitBatchResSchTelemetry(item t)))))))))
    )
  )
)

procedure(oagitBatchResReadbackSymbol(lib cell view)
  let((cv openedByScript capRes)
    cond(
      (!isCallable('oagitSymBuildResponse)
        list(nil "readback_unavailable" "oagitSymBuildResponse is not loaded"))
      (t
        cv = dbFindOpenCellViewByName(lib cell view)
        if(cv
            then openedByScript = nil
            else
              cv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        if(!cv
            then list(nil "readback_open_failed" "unable to open published symbol for readback")
            else
              capRes = errset(oagitSymBuildResponse(cv lib cell view) t)
              when(openedByScript errset(dbClose(cv) t))
              if(capRes && car(capRes)
                  then list(t car(capRes) nil)
                  else list(nil "readback_failed"
                            sprintf(nil "same-session symbol readback failed: %L" errset.errset)))))
    )
  )
)

procedure(oagitBatchResRestoreSymbol(item doReadback)
  let((target lib cell view draftCell overwrite allowLibs
       cv buildResult copyResult pubCv cleanupResult rbRes)
    target = item->target
    lib = oagitBatchResString(target->library)
    cell = oagitBatchResString(target->cell)
    view = oagitBatchResString(target->view)
    draftCell = oagitBatchResString(item->draft_cell)
    overwrite = item->overwrite
    allowLibs = item->allow_libraries

    oagitSymResErrorCode = nil
    oagitSymResErrorMessage = nil
    oagitSymResMismatches = nil
    oagitSymResPinsVerified = nil

    cond(
      (!isCallable('oagitSymResBuild) || !isCallable('oagitSymResCleanupDraft)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "symbol_restore_unavailable" "restore_symbol.il is not loaded"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (!member(lib allowLibs)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "disallowed_library" "target library is not allow-listed"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (oagitDirHasLiveLocks(lib cell view)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "live_lock" "target symbol view has a live edit lock"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (ddGetObj(lib cell view) && !overwrite
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "target_exists" "target symbol CellView already exists"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (equal(draftCell cell)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "draft_exists" "draft cell must be distinct from target cell"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (ddGetObj(lib draftCell view)
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil draftCell 0 0 nil nil nil
            "draft_exists" "draft symbol CellView already exists"
            oagitBatchResSymTelemetry(nil nil 0 0))))
      (t
        cv = dbOpenCellViewByType(lib draftCell view "schematicSymbol" "w")
        if(!cv
            then
              list(
                nil
                oagitBatchResItemResult(
                  lib cell view nil nil draftCell 0 0 nil nil nil
                  "draft_open_failed" "unable to open draft symbol CellView"
                  oagitBatchResSymTelemetry(nil nil 0 0)))
            else
              buildResult = errset(oagitSymResBuild(item cv) t)
              unless(buildResult
                oagitSymResSetFailure(
                  "symbol_restore_failed"
                  sprintf(nil "unexpected symbol restore error: %L" errset.errset))
              )
              if(oagitSymResErrorCode
                  then
                    oagitSymResCleanupDraft(lib draftCell view cv)
                    list(
                      nil
                      oagitBatchResItemResult(
                        lib cell view nil nil draftCell 0 0
                        oagitSymResPinsVerified oagitSymResMismatches nil
                        oagitSymResErrorCode oagitSymResErrorMessage
                        oagitBatchResSymTelemetry(
                          oagitSymResPinsVerified oagitSymResMismatches 0 0)))
                  else
                    copyResult = errset(dbCopyCellView(cv lib cell view nil nil t) t)
                    pubCv = nil
                    unless(copyResult && car(copyResult)
                      oagitSymResSetFailure("publish_failed" "dbCopyCellView failed for symbol")
                    )
                    if(oagitSymResErrorCode
                        then
                          oagitSymResCleanupDraft(lib draftCell view cv)
                          list(
                            nil
                            oagitBatchResItemResult(
                              lib cell view nil nil draftCell 0 0
                              oagitSymResPinsVerified oagitSymResMismatches nil
                              oagitSymResErrorCode oagitSymResErrorMessage
                              oagitBatchResSymTelemetry(
                                oagitSymResPinsVerified oagitSymResMismatches 0 0)))
                        else
                          pubCv = car(copyResult)
                          cleanupResult = oagitSymResCleanupDraft(lib draftCell view cv)
                          unless(cleanupResult
                            unless(oagitSymResErrorCode
                              oagitSymResSetFailure("cleanup_failed" "symbol draft cleanup failed")
                            )
                          )
                          if(oagitSymResErrorCode
                              then
                                oagitBatchResClosePublished(pubCv lib cell view)
                                list(
                                  nil
                                  oagitBatchResItemResult(
                                    lib cell view nil nil draftCell 0 0
                                    oagitSymResPinsVerified oagitSymResMismatches nil
                                    oagitSymResErrorCode oagitSymResErrorMessage
                                    oagitBatchResSymTelemetry(
                                      oagitSymResPinsVerified oagitSymResMismatches 0 0)))
                              else
                                if(doReadback
                                    then
                                      rbRes = oagitBatchResReadbackSymbol(lib cell view)
                                      oagitBatchResClosePublished(pubCv lib cell view)
                                      if(car(rbRes)
                                          then
                                            list(
                                              t
                                              oagitBatchResItemResult(
                                                lib cell view t t draftCell 0 0
                                                oagitSymResPinsVerified nil cadr(rbRes)
                                                nil nil
                                                oagitBatchResSymTelemetry(
                                                  oagitSymResPinsVerified nil 0 0)))
                                          else
                                            list(
                                              nil
                                              oagitBatchResItemResult(
                                                lib cell view nil t draftCell 0 0
                                                oagitSymResPinsVerified nil nil
                                                cadr(rbRes) caddr(rbRes)
                                                oagitBatchResSymTelemetry(
                                                  oagitSymResPinsVerified nil 0 0))))
                                    else
                                      oagitBatchResClosePublished(pubCv lib cell view)
                                      list(
                                        t
                                        oagitBatchResItemResult(
                                          lib cell view t t draftCell 0 0
                                          oagitSymResPinsVerified nil nil
                                          nil nil
                                          oagitBatchResSymTelemetry(
                                            oagitSymResPinsVerified nil 0 0)))))))))
    )
  )
)

procedure(oagitBatchResRestoreOne(item doReadback)
  let((target lib cell view viewType symRes)
    target = item->target
    lib = if(target oagitBatchResString(target->library) "")
    cell = if(target oagitBatchResString(target->cell) "")
    view = if(target oagitBatchResString(target->view) "")
    viewType = if(item->view_type oagitBatchResString(item->view_type) "")
    cond(
      (equal(viewType "schematic") || (equal(viewType "") && equal(view "schematic"))
        oagitBatchResRestoreSchematic(item doReadback))
      (equal(viewType "symbol") || (equal(viewType "") && equal(view "symbol"))
        if(isCallable('oagitSymResBuild)
            then oagitBatchResRestoreSymbol(item doReadback)
            else
              if(isCallable('oagitSymRestoreItem)
                  then
                    symRes = errset(oagitSymRestoreItem(item doReadback) t)
                    if(symRes && car(symRes)
                        then car(symRes)
                        else
                          list(
                            nil
                            oagitBatchResItemResult(
                              lib cell view nil nil item->draft_cell 0 0 nil nil nil
                              "symbol_restore_failed"
                              sprintf(nil "symbol restore failed: %L" errset.errset))))
                  else
                    list(
                      nil
                      oagitBatchResItemResult(
                        lib cell view nil nil item->draft_cell 0 0 nil nil nil
                        "symbol_restore_unavailable"
                        "restore_symbol.il is not loaded")))))
      (equal(viewType "layout") || (equal(viewType "") && (equal(view "layout") || rexMatchp("^layout" view)))
        if(isCallable('oagitLayoutRestoreItem)
            then
              symRes = errset(oagitLayoutRestoreItem(item doReadback) t)
              if(symRes && car(symRes)
                  then car(symRes)
                  else
                    list(
                      nil
                      oagitBatchResItemResult(
                        lib cell view nil nil item->draft_cell 0 0 nil nil nil
                        "layout_restore_failed"
                        sprintf(nil "layout restore failed: %L" errset.errset))))
            else
              list(
                nil
                oagitBatchResItemResult(
                  lib cell view nil nil item->draft_cell 0 0 nil nil nil
                  "layout_restore_unavailable"
                  "restore_layout.il is not loaded"))))
      (t
        list(
          nil
          oagitBatchResItemResult(
            lib cell view nil nil item->draft_cell 0 0 nil nil nil
            "unsupported_view"
            sprintf(nil "unsupported restore view: %s" view))))
    )
  )
)

procedure(oagitBatchRunRestore(req)
  let((protocol reqId outPath opts doReadback results errors allOk item oneRes
       daemonIdVal reusedVal sessionObj response)
    protocol = if(req->protocol oagitBatchResString(req->protocol) "oagit-batch/1")
    reqId = if(req->request_id oagitBatchResString(req->request_id) "batch-restore")
    outPath = req->output_path
    unless(outPath
      when(req->output_dir
        outPath = strcat(oagitBatchResString(req->output_dir) "/response.json")
      )
    )
    opts = req->options
    doReadback = if(opts && member('readback opts) opts->readback t)
    results = nil
    errors = nil
    allOk = t

    foreach(item req->items
      oneRes = oagitBatchResRestoreOne(item doReadback)
      unless(car(oneRes)
        allOk = nil
      )
      results = cons(cadr(oneRes) results)
    )

    daemonIdVal = if(boundp('oagitDaemonCurrentId) && oagitDaemonCurrentId
                      oagitBatchResString(oagitDaemonCurrentId)
                      list('jsonNull))
    reusedVal = if(boundp('oagitDaemonCurrentId) && oagitDaemonCurrentId t nil)
    sessionObj = list('jsonObject
                      list("daemon_id" daemonIdVal)
                      list("reused" oagitBatchResBool(reusedVal)))

    response = list('jsonObject
                    list("protocol" protocol)
                    list("request_id" reqId)
                    list("ok" oagitBatchResBool(allOk))
                    list("results" cons('jsonArray reverse(results)))
                    list("errors" cons('jsonArray reverse(errors)))
                    list("session" sessionObj))

    when(outPath
      oagitBatchResWriteAtomic(outPath response)
    )
    response
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
     oagitRequest->action == "restore_batch"
  oagitBatchRunRestore(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
