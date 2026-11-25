/* Single-session batch capture and hierarchy traversal for oa-git (oagit-batch/1).
 *
 * Captures multiple CellViews in a single Virtuoso session, with optional
 * cycle-safe, depth-bounded custom hierarchy discovery and matching custom
 * symbol capture. All functions are prefixed with oagitBatch*.
 */

procedure(oagitBatchCapString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitBatchCapBool(value)
  list('jsonBool if(value t nil))
)

procedure(oagitBatchCapCvKey(lib cell view)
  sprintf(nil "%s/%s/%s"
          oagitBatchCapString(lib)
          oagitBatchCapString(cell)
          oagitBatchCapString(view))
)

procedure(oagitBatchCapCvObject(lib cell view)
  list('jsonObject
       list("library" oagitBatchCapString(lib))
       list("cell" oagitBatchCapString(cell))
       list("view" oagitBatchCapString(view)))
)

procedure(oagitBatchCapSafeNameP(name)
  stringp(name) &&
  strlen(name) > 0 &&
  !equal(name ".") &&
  !equal(name "..") &&
  !rexMatchp("[/\\\\]" name)
)

procedure(oagitBatchCapWriteAtomic(outPath payload)
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

procedure(oagitBatchCapCloseCv(cv openedByScript)
  when(cv && openedByScript
    errset(dbClose(cv) t)
  )
)

procedure(oagitBatchCapCollectChildMasters(cv skipLibraries)
  let((pinFigIds term pin fig inst mLib mCell mView seenKey seenTable children)
    pinFigIds = makeTable("oagitBatchCapPinFigs" nil)
    seenTable = makeTable("oagitBatchCapChildSeen" nil)
    children = nil
    foreach(term cv~>terminals
      foreach(pin term~>pins
        fig = pin~>fig
        when(fig pinFigIds[fig] = t)
      )
    )
    foreach(inst cv~>instances
      unless(pinFigIds[inst]
        mLib = oagitBatchCapString(inst~>libName)
        mCell = oagitBatchCapString(inst~>cellName)
        mView = oagitBatchCapString(inst~>viewName)
        when(oagitBatchCapSafeNameP(mLib) &&
             oagitBatchCapSafeNameP(mCell) &&
             !member(mLib skipLibraries)
          seenKey = sprintf(nil "%s/%s" mLib mCell)
          unless(seenTable[seenKey]
            seenTable[seenKey] = t
            children = cons(list(mLib mCell mView) children)
          )
        )
      )
    )
    reverse(children)
  )
)

procedure(oagitBatchCapVisitHier(lib cell view depth pathList state)
  let((cvKey maxDepth skipLibraries includeSymbols visited activeStack
       cv openedByScript childMasters child cLib cCell symKey cyclePath)
    cvKey = oagitBatchCapCvKey(lib cell view)
    maxDepth = state->maxDepth
    skipLibraries = state->skipLibraries
    includeSymbols = state->includeSymbols
    visited = state->visited
    activeStack = state->activeStack

    cond(
      (activeStack[cvKey]
        cyclePath = append(pathList list(cvKey))
        state->cycles = cons(cons('jsonArray cyclePath) state->cycles)
        nil)
      (depth > maxDepth
        state->allOk = nil
        state->errors = cons(
          list('jsonObject
               list("code" "hierarchy_depth_exceeded")
               list("cellview" cvKey)
               list("depth" depth)
               list("max_depth" maxDepth)
               list("message"
                    sprintf(nil "hierarchy depth %d exceeded max_depth %d at %s"
                            depth maxDepth cvKey)))
          state->errors)
        nil)
      (visited[cvKey]
        nil)
      (t
        visited[cvKey] = t
        activeStack[cvKey] = t
        state->targets = cons(list(lib cell view) state->targets)
        when(depth > 0
          state->discovered = cons(oagitBatchCapCvObject(lib cell view) state->discovered)
        )

        when(includeSymbols && equal(view "schematic") && ddGetObj(lib cell "symbol")
          symKey = oagitBatchCapCvKey(lib cell "symbol")
          unless(visited[symKey]
            visited[symKey] = t
            state->targets = cons(list(lib cell "symbol") state->targets)
            state->discovered = cons(oagitBatchCapCvObject(lib cell "symbol") state->discovered)
          )
        )

        when(equal(view "schematic")
          cv = dbFindOpenCellViewByName(lib cell view)
          if(cv
              then openedByScript = nil
              else
                cv = dbOpenCellViewByType(lib cell view nil "r")
                openedByScript = t)
          when(cv
            childMasters = oagitBatchCapCollectChildMasters(cv skipLibraries)
            oagitBatchCapCloseCv(cv openedByScript)
            foreach(child childMasters
              cLib = car(child)
              cCell = cadr(child)
              when(ddGetObj(cLib cCell "schematic")
                oagitBatchCapVisitHier(
                  cLib cCell "schematic" depth + 1
                  append(pathList list(cvKey))
                  state)
              )
              when(includeSymbols && ddGetObj(cLib cCell "symbol")
                symKey = oagitBatchCapCvKey(cLib cCell "symbol")
                unless(visited[symKey]
                  visited[symKey] = t
                  state->targets = cons(list(cLib cCell "symbol") state->targets)
                  state->discovered = cons(
                    oagitBatchCapCvObject(cLib cCell "symbol")
                    state->discovered)
                )
              )
            )
          )
        )

        activeStack[cvKey] = nil
        t)
    )
  )
)

procedure(oagitBatchCapCaptureOne(lib cell view)
  let((cv openedByScript capRes itemObj)
    cond(
      (!oagitBatchCapSafeNameP(lib) ||
       !oagitBatchCapSafeNameP(cell) ||
       !oagitBatchCapSafeNameP(view)
        list(
          nil
          list('jsonObject
               list("cellview" oagitBatchCapCvObject(lib cell view))
               list("ok" oagitBatchCapBool(nil))
               list("error_code" "invalid_cellview")
               list("error" "invalid CellView reference"))))
      (t
        cv = dbFindOpenCellViewByName(lib cell view)
        if(cv
            then openedByScript = nil
            else
              cv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        cond(
          (!cv
            list(
              nil
              list('jsonObject
                   list("cellview" oagitBatchCapCvObject(lib cell view))
                   list("ok" oagitBatchCapBool(nil))
                   list("error_code" "open_failed")
                   list("error" "unable to open requested CellView"))))
          (cv~>modifiedButNotSaved
            oagitBatchCapCloseCv(cv openedByScript)
            list(
              nil
              list('jsonObject
                   list("cellview" oagitBatchCapCvObject(lib cell view))
                   list("ok" oagitBatchCapBool(nil))
                   list("error_code" "modified_but_not_saved")
                   list("error" "CellView has unsaved modifications"))))
          (equal(view "schematic")
            if(isCallable('oagitExportBuildResponse)
                then
                  capRes = errset(oagitExportBuildResponse(cv lib cell view) t)
                  oagitBatchCapCloseCv(cv openedByScript)
                  if(capRes && car(capRes)
                      then
                        itemObj = list('jsonObject
                                       list("cellview" oagitBatchCapCvObject(lib cell view))
                                       list("ok" oagitBatchCapBool(t))
                                       list("capture" car(capRes)))
                        list(t itemObj)
                      else
                        list(
                          nil
                          list('jsonObject
                               list("cellview" oagitBatchCapCvObject(lib cell view))
                               list("ok" oagitBatchCapBool(nil))
                               list("error_code" "capture_failed")
                               list("error"
                                    sprintf(nil "schematic capture failed: %L" errset.errset)))))
                else
                  oagitBatchCapCloseCv(cv openedByScript)
                  list(
                    nil
                    list('jsonObject
                         list("cellview" oagitBatchCapCvObject(lib cell view))
                         list("ok" oagitBatchCapBool(nil))
                         list("error_code" "schematic_capture_unavailable")
                         list("error" "oagitExportBuildResponse is not loaded")))))
          (equal(view "symbol")
            if(isCallable('oagitSymBuildResponse)
                then
                  capRes = errset(oagitSymBuildResponse(cv lib cell view) t)
                  oagitBatchCapCloseCv(cv openedByScript)
                  if(capRes && car(capRes)
                      then
                        itemObj = list('jsonObject
                                       list("cellview" oagitBatchCapCvObject(lib cell view))
                                       list("ok" oagitBatchCapBool(t))
                                       list("capture" car(capRes)))
                        list(t itemObj)
                      else
                        list(
                          nil
                          list('jsonObject
                               list("cellview" oagitBatchCapCvObject(lib cell view))
                               list("ok" oagitBatchCapBool(nil))
                               list("error_code" "capture_failed")
                               list("error"
                                    sprintf(nil "symbol capture failed: %L" errset.errset)))))
                else
                  oagitBatchCapCloseCv(cv openedByScript)
                  list(
                    nil
                    list('jsonObject
                         list("cellview" oagitBatchCapCvObject(lib cell view))
                         list("ok" oagitBatchCapBool(nil))
                         list("error_code" "symbol_capture_unavailable")
                         list("error" "oagitSymBuildResponse is not loaded")))))
          (equal(view "layout") || rexMatchp("^layout" view) || equal(cv~>cellViewType "maskLayout")
            if(isCallable('oagitLayoutExportBuildResponse)
                then
                  capRes = errset(oagitLayoutExportBuildResponse(cv lib cell view) t)
                  oagitBatchCapCloseCv(cv openedByScript)
                  if(capRes && car(capRes)
                      then
                        itemObj = list('jsonObject
                                       list("cellview" oagitBatchCapCvObject(lib cell view))
                                       list("ok" oagitBatchCapBool(t))
                                       list("capture" car(capRes)))
                        list(t itemObj)
                      else
                        list(
                          nil
                          list('jsonObject
                               list("cellview" oagitBatchCapCvObject(lib cell view))
                               list("ok" oagitBatchCapBool(nil))
                               list("error_code" "capture_failed")
                               list("error"
                                    sprintf(nil "layout capture failed: %L" errset.errset)))))
                else
                  oagitBatchCapCloseCv(cv openedByScript)
                  list(
                    nil
                    list('jsonObject
                         list("cellview" oagitBatchCapCvObject(lib cell view))
                         list("ok" oagitBatchCapBool(nil))
                         list("error_code" "layout_capture_unavailable")
                         list("error" "oagitLayoutExportBuildResponse is not loaded")))))
          (t
            oagitBatchCapCloseCv(cv openedByScript)
            list(
              nil
              list('jsonObject
                   list("cellview" oagitBatchCapCvObject(lib cell view))
                   list("ok" oagitBatchCapBool(nil))
                   list("error_code" "unsupported_view")
                   list("error" sprintf(nil "unsupported view type: %s" view)))))
        ))
    )
  )
)

procedure(oagitBatchRunCapture(req)
  let((reqId protocol outPath opts hier maxDepth includeSymbols skipLibraries
       state entry lib cell view targetList results oneRes symKey
       daemonIdVal reusedVal sessionObj response)
    protocol = if(req->protocol oagitBatchCapString(req->protocol) "oagit-batch/1")
    reqId = if(req->request_id oagitBatchCapString(req->request_id) "batch-capture")
    outPath = req->output_path
    unless(outPath
      when(req->output_dir
        outPath = strcat(oagitBatchCapString(req->output_dir) "/response.json")
      )
    )
    opts = req->options
    hier = if(opts opts->hier nil)
    maxDepth = if(opts && integerp(opts->max_depth) opts->max_depth 32)
    includeSymbols = if(opts opts->include_symbols nil)
    skipLibraries = if(opts && member('skip_libraries opts)
                        opts->skip_libraries
                        list("basic" "analogLib" "cdsDefTechLib"))

    state = list(nil
      'maxDepth maxDepth
      'skipLibraries skipLibraries
      'includeSymbols includeSymbols
      'visited makeTable("oagitBatchVisited" nil)
      'activeStack makeTable("oagitBatchActive" nil)
      'targets nil
      'discovered nil
      'cycles nil
      'errors nil
      'allOk t
    )

    foreach(entry req->cellviews
      lib = oagitBatchCapString(entry->library)
      cell = oagitBatchCapString(entry->cell)
      view = oagitBatchCapString(entry->view)
      if(hier
          then
            oagitBatchCapVisitHier(lib cell view 0 nil state)
          else
            unless(state->visited[oagitBatchCapCvKey(lib cell view)]
              state->visited[oagitBatchCapCvKey(lib cell view)] = t
              state->targets = cons(list(lib cell view) state->targets)
            )
            when(includeSymbols && equal(view "schematic") && ddGetObj(lib cell "symbol")
              symKey = oagitBatchCapCvKey(lib cell "symbol")
              unless(state->visited[symKey]
                state->visited[symKey] = t
                state->targets = cons(list(lib cell "symbol") state->targets)
                state->discovered = cons(
                  oagitBatchCapCvObject(lib cell "symbol")
                  state->discovered)
              )
            ))
    )

    targetList = reverse(state->targets)
    results = nil
    foreach(entry targetList
      lib = car(entry)
      cell = cadr(entry)
      view = caddr(entry)
      oneRes = oagitBatchCapCaptureOne(lib cell view)
      unless(car(oneRes)
        state->allOk = nil
      )
      results = cons(cadr(oneRes) results)
    )

    daemonIdVal = if(boundp('oagitDaemonCurrentId) && oagitDaemonCurrentId
                      oagitBatchCapString(oagitDaemonCurrentId)
                      list('jsonNull))
    reusedVal = if(boundp('oagitDaemonCurrentId) && oagitDaemonCurrentId t nil)
    sessionObj = list('jsonObject
                      list("daemon_id" daemonIdVal)
                      list("reused" oagitBatchCapBool(reusedVal)))

    response = list('jsonObject
                    list("protocol" protocol)
                    list("request_id" reqId)
                    list("ok" oagitBatchCapBool(state->allOk))
                    list("results" cons('jsonArray reverse(results)))
                    list("discovered" cons('jsonArray reverse(state->discovered)))
                    list("cycles" cons('jsonArray reverse(state->cycles)))
                    list("errors" cons('jsonArray reverse(state->errors)))
                    list("session" sessionObj))

    when(outPath
      oagitBatchCapWriteAtomic(outPath response)
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
     oagitRequest->action == "capture_batch"
  oagitBatchRunCapture(oagitRequest)
  when(equal(getShellEnvVar("OAGIT_AUTO_EXIT") "1") &&
       stringp(oagitRequest->output_path) && isFile(oagitRequest->output_path)
    exit(0))
)
