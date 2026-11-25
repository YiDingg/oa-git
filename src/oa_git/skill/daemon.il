/* Lazy auto-sleep headless Virtuoso spool daemon for oa-git (oagit-spool/1).
 *
 * Loads oagit_common.il and available action scripts once at startup, polls the
 * 0700 spool directory for sorted *.req.il files, dispatches ONLY allowlisted
 * actions, validates that output_path stays strictly inside spool_dir, writes
 * responses atomically (*.resp.json.tmp -> *.resp.json), and cleanly exits
 * after idle_timeout_s seconds of inactivity or upon receiving action "stop".
 */

procedure(oagitDaemonString(value)
  cond(
    (stringp(value) value)
    (symbolp(value) sprintf(nil "%s" value))
    (!value "")
    (t sprintf(nil "%L" value))
  )
)

procedure(oagitDaemonBool(value)
  list('jsonBool if(value t nil))
)

procedure(oagitDaemonNowSeconds()
  let((res)
    res = errset(stringToTime(getCurrentTime()) t)
    if(res && integerp(car(res)) car(res) 0)
  )
)

procedure(oagitDaemonStripTrailingSlash(pathText)
  let((n)
    n = strlen(pathText)
    while(n > 1 && equal(substring(pathText n 1) "/")
      pathText = substring(pathText 1 n - 1)
      n = strlen(pathText)
    )
    pathText
  )
)

procedure(oagitDaemonSafePathP(targetPath rootDir)
  let((normTarget normRoot prefix pLen)
    cond(
      (!stringp(targetPath) || !stringp(rootDir) nil)
      (strlen(targetPath) == 0 || strlen(rootDir) == 0 nil)
      (rexMatchp("\\\\" targetPath) nil)
      (pcreMatchp("(^|/)\\.\\.(/|$)" targetPath) nil)
      (t
        normTarget = simplifyFilename(targetPath nil)
        normRoot = simplifyFilename(rootDir nil)
        when(stringp(normRoot)
          normRoot = oagitDaemonStripTrailingSlash(normRoot)
        )
        if(stringp(normTarget) && stringp(normRoot)
            then
              prefix = strcat(normRoot "/")
              pLen = strlen(prefix)
              strlen(normTarget) > pLen &&
              equal(substring(normTarget 1 pLen) prefix)
            else nil))
    )
  )
)

procedure(oagitDaemonSafeNameP(name)
  stringp(name) &&
  strlen(name) > 0 &&
  !equal(name ".") &&
  !equal(name "..") &&
  !rexMatchp("[/\\\\]" name)
)

procedure(oagitDaemonAllowlistedActionP(action)
  stringp(action) &&
  member(action list(
    "ping"
    "stop"
    "capture_schematic"
    "restore_schematic"
    "capture_symbol"
    "restore_symbol"
    "capture_layout"
    "restore_layout"
    "capture_batch"
    "restore_batch"
  ))
)

procedure(oagitDaemonWriteAtomicJson(destPath payload)
  let((tmpPath written renamed)
    tmpPath = strcat(destPath ".tmp")
    written = errset(oagitWriteJsonFile(tmpPath payload) t)
    if(written && car(written)
        then
          renamed = errset(renameFile(tmpPath destPath) t)
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

procedure(oagitDaemonWriteState(stateFile daemonId stateName extraPairs)
  let((pairs pidRes pidVal)
    pidRes = errset(ipcGetPid() t)
    pidVal = if(pidRes && integerp(car(pidRes)) car(pidRes) 0)
    pairs = list(
      list("protocol" "oagit-spool/1")
      list("daemon_id" oagitDaemonString(daemonId))
      list("state" oagitDaemonString(stateName))
      list("pid" pidVal)
      list("updated_at" oagitDaemonNowSeconds())
    )
    when(extraPairs
      pairs = append(pairs extraPairs)
    )
    oagitDaemonWriteAtomicJson(stateFile cons('jsonObject pairs))
  )
)

procedure(oagitDaemonTouchHeartbeat(heartbeatFile daemonId)
  let((tmpPath port)
    tmpPath = strcat(heartbeatFile ".tmp")
    port = outfile(tmpPath "w")
    when(port
      fprintf(port "%d %s\n" oagitDaemonNowSeconds() oagitDaemonString(daemonId))
      close(port)
      errset(renameFile(tmpPath heartbeatFile) t)
    )
  )
)

procedure(oagitDaemonCloseReadOnlyCellViews()
  let((openCvs cv)
    openCvs = errset(dbGetOpenCellViews() t)
    when(openCvs && listp(car(openCvs))
      foreach(cv car(openCvs)
        when(cv && equal(cv~>mode "r") && !cv~>modifiedButNotSaved
          errset(dbClose(cv) t)
        )
      )
    )
    t
  )
)

procedure(oagitDaemonLoadScripts(skillDir)
  let((scripts scriptName fullPath prevReq)
    prevReq = nil
    when(boundp('oagitRequest)
      prevReq = oagitRequest
    )
    oagitRequest = nil
    scripts = list(
      "oagit_common.il"
      "export_schematic.il"
      "restore_schematic.il"
      "export_symbol.il"
      "restore_symbol.il"
      "export_layout.il"
      "restore_layout.il"
      "batch_capture.il"
      "batch_restore.il"
    )
    foreach(scriptName scripts
      fullPath = strcat(oagitDaemonStripTrailingSlash(skillDir) "/" scriptName)
      when(isFile(fullPath)
        errset(load(fullPath) t)
      )
    )
    oagitRequest = prevReq
    t
  )
)

procedure(oagitDaemonErrorPayload(reqId protocol code message daemonId
                                  @optional (action nil))
  let((pairs isBatch)
    isBatch = equal(oagitDaemonString(protocol) "oagit-batch/1") ||
              equal(action "capture_batch") ||
              equal(action "restore_batch")
    pairs = list(
      list("protocol" oagitDaemonString(protocol))
      list("request_id" oagitDaemonString(reqId))
      list("ok" oagitDaemonBool(nil))
      list("error_code" oagitDaemonString(code))
      list("error" oagitDaemonString(message))
      list("session"
           list('jsonObject
                list("daemon_id" oagitDaemonString(daemonId))
                list("reused" oagitDaemonBool(t))))
    )
    when(isBatch
      pairs = append(
        pairs
        list(
          list("results" list('jsonArray))
          list("discovered" list('jsonArray))
          list("cycles" list('jsonArray))
          list("errors" list('jsonArray))
        )
      )
    )
    cons('jsonObject pairs)
  )
)

procedure(oagitDaemonCaptureSingleSchematic(req reqId daemonId outPath)
  let((lib cell view cv openedByScript capRes response)
    lib = req->library
    cell = req->cell
    view = req->view
    cond(
      (!oagitDaemonSafeNameP(lib) || !oagitDaemonSafeNameP(cell) || !equal(view "schematic")
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "invalid_cellview"
                                  "invalid schematic cellview reference" daemonId)))
      (!isCallable('oagitExportBuildResponse)
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                  "export_schematic.il is not loaded" daemonId)))
      (t
        cv = dbFindOpenCellViewByName(lib cell view)
        if(cv
            then openedByScript = nil
            else
              cv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        cond(
          (!cv
            oagitDaemonWriteAtomicJson(
              outPath
              oagitDaemonErrorPayload(reqId "oagit-spool/1" "open_failed"
                                      "unable to open requested CellView" daemonId)))
          (cv~>modifiedButNotSaved
            when(openedByScript errset(dbClose(cv) t))
            oagitDaemonWriteAtomicJson(
              outPath
              oagitDaemonErrorPayload(reqId "oagit-spool/1" "modified_but_not_saved"
                                      "CellView has unsaved modifications" daemonId)))
          (t
            capRes = errset(oagitExportBuildResponse(cv lib cell view) t)
            when(openedByScript errset(dbClose(cv) t))
            if(capRes && car(capRes)
                then
                  response = list('jsonObject
                                  list("protocol" "oagit-spool/1")
                                  list("request_id" oagitDaemonString(reqId))
                                  list("ok" oagitDaemonBool(t))
                                  list("action" "capture_schematic")
                                  list("capture" car(capRes))
                                  list("session"
                                       list('jsonObject
                                            list("daemon_id" oagitDaemonString(daemonId))
                                            list("reused" oagitDaemonBool(t)))))
                  oagitDaemonWriteAtomicJson(outPath response)
                else
                  oagitDaemonWriteAtomicJson(
                    outPath
                    oagitDaemonErrorPayload(
                      reqId "oagit-spool/1" "capture_failed"
                      sprintf(nil "schematic capture failed: %L" errset.errset)
                      daemonId)))))
      )
    )
  )
)

procedure(oagitDaemonCaptureSingleSymbol(req reqId daemonId outPath)
  let((lib cell view cv openedByScript capRes response)
    lib = req->library
    cell = req->cell
    view = if(req->view req->view "symbol")
    cond(
      (!oagitDaemonSafeNameP(lib) || !oagitDaemonSafeNameP(cell) || !equal(view "symbol")
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "invalid_cellview"
                                  "invalid symbol cellview reference" daemonId)))
      (!isCallable('oagitSymBuildResponse)
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                  "export_symbol.il is not loaded" daemonId)))
      (t
        cv = dbFindOpenCellViewByName(lib cell view)
        if(cv
            then openedByScript = nil
            else
              cv = dbOpenCellViewByType(lib cell view nil "r")
              openedByScript = t)
        cond(
          (!cv
            oagitDaemonWriteAtomicJson(
              outPath
              oagitDaemonErrorPayload(reqId "oagit-spool/1" "open_failed"
                                      "unable to open symbol CellView" daemonId)))
          (cv~>modifiedButNotSaved
            when(openedByScript errset(dbClose(cv) t))
            oagitDaemonWriteAtomicJson(
              outPath
              oagitDaemonErrorPayload(reqId "oagit-spool/1" "modified_but_not_saved"
                                      "symbol CellView has unsaved modifications" daemonId)))
          (t
            capRes = errset(oagitSymBuildResponse(cv lib cell view) t)
            when(openedByScript errset(dbClose(cv) t))
            if(capRes && car(capRes)
                then
                  response = list('jsonObject
                                  list("protocol" "oagit-spool/1")
                                  list("request_id" oagitDaemonString(reqId))
                                  list("ok" oagitDaemonBool(t))
                                  list("action" "capture_symbol")
                                  list("capture" car(capRes))
                                  list("session"
                                       list('jsonObject
                                            list("daemon_id" oagitDaemonString(daemonId))
                                            list("reused" oagitDaemonBool(t)))))
                  oagitDaemonWriteAtomicJson(outPath response)
                else
                  oagitDaemonWriteAtomicJson(
                    outPath
                    oagitDaemonErrorPayload(
                      reqId "oagit-spool/1" "capture_failed"
                      sprintf(nil "symbol capture failed: %L" errset.errset)
                      daemonId)))))
      )
    )
  )
)

procedure(oagitDaemonDispatch(req reqId daemonId spoolDir defaultOutPath)
  let((action outPath stopFlag pidRes pidVal callRes)
    action = req->action
    outPath = if(req->output_path req->output_path defaultOutPath)
    stopFlag = nil
    when(oagitDaemonSafePathP(outPath spoolDir) &&
         oagitDaemonAllowlistedActionP(action) &&
         !member(action list("ping" "stop")) &&
         isCallable('ddUpdateLibList)
      errset(ddUpdateLibList() t)
    )
    cond(
      (!oagitDaemonSafePathP(outPath spoolDir)
        oagitDaemonWriteAtomicJson(
          defaultOutPath
          oagitDaemonErrorPayload(
            reqId
            if(member(action list("capture_batch" "restore_batch"))
                "oagit-batch/1" "oagit-spool/1")
            "unsafe_output_path"
            "request output_path escapes daemon spool directory"
            daemonId action)))
      (!oagitDaemonAllowlistedActionP(action)
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "unsupported_action"
                                  sprintf(nil "action not allowlisted: %L" action)
                                  daemonId)))
      (equal(action "ping")
        pidRes = errset(ipcGetPid() t)
        pidVal = if(pidRes && integerp(car(pidRes)) car(pidRes) 0)
        oagitDaemonWriteAtomicJson(
          outPath
          list('jsonObject
               list("protocol" "oagit-spool/1")
               list("request_id" oagitDaemonString(reqId))
               list("ok" oagitDaemonBool(t))
               list("pong" oagitDaemonBool(t))
               list("daemon_id" oagitDaemonString(daemonId))
               list("pid" pidVal))))
      (equal(action "stop")
        oagitDaemonWriteAtomicJson(
          outPath
          list('jsonObject
               list("protocol" "oagit-spool/1")
               list("request_id" oagitDaemonString(reqId))
               list("ok" oagitDaemonBool(t))
               list("stopping" oagitDaemonBool(t))
               list("daemon_id" oagitDaemonString(daemonId))))
        stopFlag = t)
      (equal(action "capture_batch")
        if(isCallable('oagitBatchRunCapture)
            then
              callRes = errset(oagitBatchRunCapture(req) t)
              unless(callRes && isFile(outPath)
                oagitDaemonWriteAtomicJson(
                  outPath
                  oagitDaemonErrorPayload(
                    reqId "oagit-batch/1" "batch_capture_failed"
                    sprintf(nil "capture_batch failed: %L" errset.errset)
                    daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-batch/1" "action_unavailable"
                                        "batch_capture.il is not loaded" daemonId))))
      (equal(action "restore_batch")
        if(isCallable('oagitBatchRunRestore)
            then
              callRes = errset(oagitBatchRunRestore(req) t)
              unless(callRes && isFile(outPath)
                oagitDaemonWriteAtomicJson(
                  outPath
                  oagitDaemonErrorPayload(
                    reqId "oagit-batch/1" "batch_restore_failed"
                    sprintf(nil "restore_batch failed: %L" errset.errset)
                    daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-batch/1" "action_unavailable"
                                        "batch_restore.il is not loaded" daemonId))))
      (equal(action "capture_schematic")
        oagitDaemonCaptureSingleSchematic(req reqId daemonId outPath))
      (equal(action "capture_symbol")
        oagitDaemonCaptureSingleSymbol(req reqId daemonId outPath))
      (equal(action "restore_schematic")
        if(isCallable('oagitBatchResRestoreOne)
            then
              callRes = errset(oagitBatchResRestoreOne(req if(req->readback t nil)) t)
              if(callRes && listp(car(callRes)) && cadr(car(callRes))
                  then
                    oagitDaemonWriteAtomicJson(
                      outPath
                      list('jsonObject
                           list("protocol" "oagit-spool/1")
                           list("request_id" oagitDaemonString(reqId))
                           list("ok" oagitDaemonBool(car(car(callRes))))
                           list("action" "restore_schematic")
                           list("result" cadr(car(callRes)))
                           list("session"
                                list('jsonObject
                                     list("daemon_id" oagitDaemonString(daemonId))
                                     list("reused" oagitDaemonBool(t))))))
                  else
                    oagitDaemonWriteAtomicJson(
                      outPath
                      oagitDaemonErrorPayload(
                        reqId "oagit-spool/1" "restore_failed"
                        sprintf(nil "restore_schematic failed: %L" errset.errset)
                        daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                        "batch_restore.il is not loaded" daemonId))))
      (equal(action "restore_symbol")
        if(isCallable('oagitBatchResRestoreOne)
            then
              callRes = errset(oagitBatchResRestoreOne(req if(req->readback t nil)) t)
              if(callRes && listp(car(callRes)) && cadr(car(callRes))
                  then
                    oagitDaemonWriteAtomicJson(
                      outPath
                      list('jsonObject
                           list("protocol" "oagit-spool/1")
                           list("request_id" oagitDaemonString(reqId))
                           list("ok" oagitDaemonBool(car(car(callRes))))
                           list("action" "restore_symbol")
                           list("result" cadr(car(callRes)))
                           list("session"
                                list('jsonObject
                                     list("daemon_id" oagitDaemonString(daemonId))
                                     list("reused" oagitDaemonBool(t))))))
                  else
                    oagitDaemonWriteAtomicJson(
                      outPath
                      oagitDaemonErrorPayload(
                        reqId "oagit-spool/1" "restore_symbol_failed"
                        sprintf(nil "restore_symbol failed: %L" errset.errset)
                        daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                        "batch_restore.il is not loaded" daemonId))))
      (equal(action "capture_layout")
        if(isCallable('oagitLayoutRunExport)
            then
              callRes = errset(oagitLayoutRunExport(req) t)
              unless(callRes && isFile(outPath)
                oagitDaemonWriteAtomicJson(
                  outPath
                  oagitDaemonErrorPayload(
                    reqId "oagit-spool/1" "capture_layout_failed"
                    sprintf(nil "capture_layout failed: %L" errset.errset)
                    daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                        "export_layout.il is not loaded" daemonId))))
      (equal(action "restore_layout")
        if(isCallable('oagitLayoutRunRestore)
            then
              callRes = errset(oagitLayoutRunRestore(req) t)
              unless(callRes && isFile(outPath)
                oagitDaemonWriteAtomicJson(
                  outPath
                  oagitDaemonErrorPayload(
                    reqId "oagit-spool/1" "restore_layout_failed"
                    sprintf(nil "restore_layout failed: %L" errset.errset)
                    daemonId)))
            else
              oagitDaemonWriteAtomicJson(
                outPath
                oagitDaemonErrorPayload(reqId "oagit-spool/1" "action_unavailable"
                                        "restore_layout.il is not loaded" daemonId))))
      (t
        oagitDaemonWriteAtomicJson(
          outPath
          oagitDaemonErrorPayload(reqId "oagit-spool/1" "unsupported_action"
                                  "unhandled action" daemonId)))
    )
    stopFlag
  )
)

procedure(oagitDaemonProcessSpoolFile(spoolDir fileName daemonId)
  let((baseLen reqId reqPath procPath defaultRespPath renRes loadRes stopFlag dispRes
       reqAction reqProto)
    stopFlag = nil
    baseLen = strlen(fileName) - 7
    when(baseLen > 0
      reqId = substring(fileName 1 baseLen)
      reqPath = strcat(spoolDir "/" fileName)
      procPath = strcat(spoolDir "/" reqId ".processing.il")
      defaultRespPath = strcat(spoolDir "/" reqId ".resp.json")
      renRes = errset(renameFile(reqPath procPath) t)
      when(renRes && car(renRes)
        oagitRequest = nil
        reqAction = nil
        reqProto = "oagit-spool/1"
        loadRes = errset(load(procPath) t)
        if(loadRes && boundp('oagitRequest) && listp(oagitRequest) && oagitRequest
            then
              reqAction = oagitRequest->action
              when(member(reqAction list("capture_batch" "restore_batch"))
                reqProto = "oagit-batch/1"
              )
              dispRes = errset(
                oagitDaemonDispatch(oagitRequest reqId daemonId spoolDir defaultRespPath)
                t)
              if(dispRes
                  then stopFlag = car(dispRes)
                  else
                    oagitDaemonWriteAtomicJson(
                      defaultRespPath
                      oagitDaemonErrorPayload(
                        reqId reqProto "dispatch_exception"
                        sprintf(nil "unhandled dispatch error: %L" errset.errset)
                        daemonId reqAction)))
            else
              oagitDaemonWriteAtomicJson(
                defaultRespPath
                oagitDaemonErrorPayload(
                  reqId "oagit-spool/1" "request_load_failed"
                  sprintf(nil "failed to load request file: %L" errset.errset)
                  daemonId)))
        unless(isFile(defaultRespPath)
          oagitDaemonWriteAtomicJson(
            defaultRespPath
            oagitDaemonErrorPayload(
              reqId reqProto "missing_response"
              "action completed without producing a response file"
              daemonId reqAction))
        )
        oagitRequest = nil
        errset(deleteFile(procPath) t)
      )
    )
    stopFlag
  )
)

procedure(oagitDaemonListPendingRequests(spoolDir)
  let((entries pending entry)
    pending = nil
    entries = errset(getDirFiles(spoolDir) t)
    when(entries && listp(car(entries))
      foreach(entry car(entries)
        when(stringp(entry) && rexMatchp("\\.req\\.il$" entry)
          pending = cons(entry pending)
        )
      )
    )
    sort(pending 'alphalessp)
  )
)

procedure(oagitDaemonPollSleep(pollMs)
  let((secCount sleepRes)
    cond(
      (!integerp(pollMs) || pollMs <= 0
        ipcSleep(1))
      (pollMs >= 1000
        secCount = pollMs / 1000
        when(secCount < 1 secCount = 1)
        ipcSleep(secCount))
      (t
        sleepRes = errset(system(sprintf(nil "sleep 0.%03d" pollMs)) t)
        unless(sleepRes
          ipcSleep(1)))
    )
  )
)

procedure(oagitDaemonMain(config)
  let((daemonId spoolDir stateFile heartbeatFile skillDir idleTimeout pollMs
       lastActive lastHeartbeat nowSec stopRequested pending fileName fileStop)
    daemonId = oagitDaemonString(config->daemon_id)
    spoolDir = oagitDaemonStripTrailingSlash(oagitDaemonString(config->spool_dir))
    stateFile = oagitDaemonString(config->state_file)
    heartbeatFile = oagitDaemonString(config->heartbeat_file)
    skillDir = oagitDaemonStripTrailingSlash(oagitDaemonString(config->skill_dir))
    idleTimeout = if(integerp(config->idle_timeout_s) config->idle_timeout_s 3600)
    pollMs = if(integerp(config->poll_ms) config->poll_ms 100)

    oagitDaemonCurrentId = daemonId
    oagitDaemonWriteState(stateFile daemonId "starting" nil)
    oagitDaemonLoadScripts(skillDir)
    oagitDaemonWriteState(stateFile daemonId "ready" nil)
    oagitDaemonTouchHeartbeat(heartbeatFile daemonId)

    lastActive = oagitDaemonNowSeconds()
    lastHeartbeat = lastActive
    stopRequested = nil

    while(!stopRequested
      pending = oagitDaemonListPendingRequests(spoolDir)
      if(pending
          then
            foreach(fileName pending
              unless(stopRequested
                fileStop = oagitDaemonProcessSpoolFile(spoolDir fileName daemonId)
                lastActive = oagitDaemonNowSeconds()
                oagitDaemonTouchHeartbeat(heartbeatFile daemonId)
                lastHeartbeat = lastActive
                when(fileStop stopRequested = t)
              )
            )
          else
            nowSec = oagitDaemonNowSeconds()
            when((nowSec - lastHeartbeat) >= 5
              oagitDaemonTouchHeartbeat(heartbeatFile daemonId)
              lastHeartbeat = nowSec
            )
            if(idleTimeout > 0 && (nowSec - lastActive) >= idleTimeout
                then stopRequested = t
                else
                  if((nowSec - lastActive) >= 5
                      then ipcSleep(1)
                      else oagitDaemonPollSleep(pollMs))))
    )

    oagitDaemonWriteState(stateFile daemonId "stopping" nil)
    oagitDaemonCloseReadOnlyCellViews()
    exit(0)
  )
)
