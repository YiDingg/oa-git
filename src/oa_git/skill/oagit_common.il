/* Shared, deterministic JSON and safety helpers for the oa-git SKILL bridge. */

procedure(oagitJsonEscape(text)
  let((i n ch chSym code out codeRes)
    i = 1
    n = strlen(text)
    out = ""
    while(i <= n
      ch = substring(text i 1)
      cond(
        (ch == "\\" out = strcat(out "\\\\"))
        (ch == "\"" out = strcat(out "\\\""))
        (ch == "\n" out = strcat(out "\\n"))
        (ch == "\r" out = strcat(out "\\r"))
        (ch == "\t" out = strcat(out "\\t"))
        (t
          chSym = getchar(text i)
          codeRes = errset(charToInt(chSym) t)
          code = if(codeRes && integerp(car(codeRes)) car(codeRes) 32)
          if(code < 32
              then out = strcat(out sprintf(nil "\\u%04x" code))
              else out = strcat(out ch)))
      )
      i = i + 1
    )
    out
  )
)

procedure(oagitJsonFiniteFloatP(value)
  let((printed)
    printed = sprintf(nil "%g" value)
    !rexMatchp("[nN][aA][nN]" printed) &&
    !rexMatchp("[iI][nN][fF]" printed)
  )
)

procedure(oagitJsonEncodeArray(items)
  let((out first)
    out = "["
    first = t
    foreach(item items
      unless(first out = strcat(out ","))
      out = strcat(out oagitJsonEncode(item))
      first = nil
    )
    strcat(out "]")
  )
)

procedure(oagitJsonEncodeObject(pairs)
  let((out first key keyText pair)
    out = "{"
    first = t
    foreach(pair pairs
      unless(pair error("JSON object member must be a key/value pair"))
      unless(first out = strcat(out ","))
      key = car(pair)
      keyText = cond(
        (stringp(key) key)
        (symbolp(key) sprintf(nil "%s" key))
        (t error("JSON object key must be a string or symbol"))
      )
      out = strcat(out "\"" oagitJsonEscape(keyText) "\":"
                   oagitJsonEncode(cadr(pair)))
      first = nil
    )
    strcat(out "}")
  )
)

procedure(oagitJsonEncode(value)
  cond(
    (listp(value) && eq(car(value) 'jsonNull) "null")
    (listp(value) && eq(car(value) 'jsonBool)
      if(cadr(value) "true" "false"))
    (listp(value) && eq(car(value) 'jsonArray)
      oagitJsonEncodeArray(cdr(value)))
    (listp(value) && eq(car(value) 'jsonObject)
      oagitJsonEncodeObject(cdr(value)))
    (stringp(value)
      strcat("\"" oagitJsonEscape(value) "\""))
    (integerp(value) sprintf(nil "%d" value))
    (floatp(value)
      if(oagitJsonFiniteFloatP(value)
          sprintf(nil "%g" value)
          error("cannot encode a non-finite JSON number")))
    (t error("unsupported value passed to oagitJsonEncode"))
  )
)

procedure(oagitWriteJsonFile(path value)
  let((port)
    port = outfile(path "w")
    when(port
      fprintf(port "%s" oagitJsonEncode(value))
      close(port)
      t
    )
  )
)

procedure(oagitFail(outPath code message)
  when(outPath
    errset(
      oagitWriteJsonFile(
        outPath
        list(
          'jsonObject
          list("ok" list('jsonBool nil))
          list("error" message)
          list("error_code" code)
        )
      )
      t
    )
  )
  nil
)

procedure(oagitDirHasLiveLocks(lib cell view)
  /* Check view directory for active *.cdslck* and *.lck edit lock files. */
  let((viewDd dir entries found)
    viewDd = ddGetObj(lib cell view)
    found = nil
    when(viewDd
      dir = viewDd~>writePath
      unless(dir dir = viewDd~>readPath)
      when(dir && isDir(dir)
        entries = getDirFiles(dir)
        foreach(entry entries
          when(stringp(entry) &&
               (rexMatchp("\\.cdslck" entry) || rexMatchp("\\.lck$" entry))
            found = t
          )
        )
      )
    )
    found
  )
)
