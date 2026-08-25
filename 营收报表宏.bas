Attribute VB_Name = "营收报表宏"
'=============================================================
' 营收报表自动计算宏
'
' 数据流：
'   4.PKG Group分类 + 5.筛选条件 + 6.PLAN + 8.单价 + 9.原始数据 + 10.异常
'      -> 宏自动生成 -> 1.汇总 / 2.入料 / 3.出货
'
' 使用步骤：
'   1. 把本文件导入：Alt+F11 -> 文件 -> 导入文件
'   2. 维护数据表：4/5/6/8/9/10（9号表把数据库数据整列粘贴）
'   3. 运行【刷新报表】生成 1/2/3 表
'   4. 跨月：运行【新建月份】，输入如 2026-09，自动补日期列并刷新
'
' 约定：
'   - 汇总表A1 为报表月份（如 2026-08-01），宏据此重建整月报表
'   - 异常只影响计算，不改动 6.PLAN
'   - 单价缺失时向前沿用最近有效单价
'   - 筛选条件/原始数据按“表头名”匹配，新增条件列无需改代码
'=============================================================
Option Explicit

Private Const S_SUMMARY As String = "1.汇总"
Private Const S_RECV As String = "2.入料"
Private Const S_SHIP As String = "3.出货"
Private Const S_PKG As String = "4.PKG Group分类"
Private Const S_FILTER As String = "5.入料、出货数据筛选条件"
Private Const S_PLAN As String = "6.PLAN"
Private Const S_PRICE As String = "8.单价"
Private Const S_RAW As String = "9.入料、出货原始数据"
Private Const S_EXC As String = "10.异常"

Private mStart As Date
Private mEnd As Date
Private mDays As Long

Private dictPG As Object        ' product -> group
Private dictGI As Object        ' group -> index
Private dictPI As Object        ' product -> index
Private products() As String
Private groups() As String

Private dictPrice As Object     ' product|day -> 单价(向前补齐)
Private dictPlanIn As Object    ' product|day -> plan in(含异常调整)
Private dictPlanOut As Object   ' product|day -> plan out(含异常调整)
Private dictRecvQty As Object   ' product|day -> 入料量
Private dictShipQty As Object   ' product|day -> 出货量

'=============================================================
' 主宏1：刷新报表
'=============================================================
Public Sub 刷新报表()
    RefreshCore 0
End Sub

'=============================================================
' 主宏2：新建月份（输入月份，补日期列并刷新）
'=============================================================
Public Sub 新建月份()
    Dim ym As String
    ym = InputBox("请输入报表月份，格式如 2026-09（留空则用当前月份）：", "新建月份", Format(DateAdd("m", 1, Date), "yyyy-mm"))
    If ym = "" Then Exit Sub
    Dim m As Date
    m = ParseYearMonth(ym)
    If m = 0 Then
        MsgBox "月份格式不正确，示例：2026-09", vbExclamation
        Exit Sub
    End If
    mStart = DateSerial(Year(m), Month(m), 1)
    mEnd = DateSerial(Year(m), Month(m) + 1, 0)
    mDays = mEnd - mStart + 1
    ' 为输入表(6.PLAN / 8.单价)补缺失的月份日期列
    EnsureInputDateHeaders ThisWorkbook.Sheets(S_PLAN), 1, 2
    EnsureInputDateHeaders ThisWorkbook.Sheets(S_PRICE), 2, 2
    RefreshCore m
End Sub

'=============================================================
' 核心刷新
'=============================================================
Private Sub RefreshCore(Optional ByVal forceMonth As Date = 0)
    On Error GoTo ErrH
    Dim msgs As Collection
    Set msgs = New Collection

    Application.ScreenUpdating = False
    Application.Calculation = xlCalculationManual
    Application.EnableEvents = False

    Dim wb As Workbook
    Set wb = ThisWorkbook
    Dim wsSum As Worksheet
    Set wsSum = wb.Sheets(S_SUMMARY)

    ' 月份
    Dim m As Date
    If forceMonth <> 0 Then
        m = forceMonth
    Else
        Dim a1v As Variant
        a1v = wsSum.Range("A1").Value
        If IsDate(a1v) Then
            m = CDate(a1v)
        Else
            m = ParseYearMonth(CStr(a1v))
            If m = 0 Then m = Date
        End If
    End If
    mStart = DateSerial(Year(m), Month(m), 1)
    mEnd = DateSerial(Year(m), Month(m) + 1, 0)
    mDays = mEnd - mStart + 1

    LoadPkgMapping wb, msgs
    If UBound(products) < 0 Or UBound(groups) < 0 Then
        msgs.Add "4.PKG Group分类 表为空，无法生成报表。"
        GoTo Cleanup
    End If

    LoadPrice wb, msgs
    LoadPlan wb, msgs
    ApplyExceptions wb, msgs
    LoadFiltersAndRaw wb, msgs
    ComputeAndWriteSheets wb, msgs

    wsSum.Range("A1").Value = mStart
    wsSum.Range("A1").NumberFormat = "yyyy-mm"
    msgs.Add "生成完成：报表月份 " & Format(mStart, "yyyy-mm")

Cleanup:
    Application.ScreenUpdating = True
    Application.Calculation = xlCalculationAutomatic
    Application.EnableEvents = True
    Dim msg As String
    Dim i As Long
    For i = 1 To msgs.Count
        msg = msg & msgs(i) & vbCrLf
    Next i
    If msg <> "" Then MsgBox msg, vbInformation
    Exit Sub
ErrH:
    MsgBox "运行出错：" & Err.Description, vbCritical
    Resume Cleanup
End Sub

'=============================================================
' 4.PKG Group分类 -> 产品/组清单
'=============================================================
Private Sub LoadPkgMapping(wb As Workbook, msgs As Collection)
    Dim ws As Worksheet
    Set ws = wb.Sheets(S_PKG)
    Dim wsSum As Worksheet
    Set wsSum = wb.Sheets(S_SUMMARY)

    Set dictPG = CreateObject("Scripting.Dictionary")
    Dim seen As Object
    Set seen = CreateObject("Scripting.Dictionary")

    Dim lastR As Long, r As Long, n As Long
    Dim p As String, g As String
    lastR = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row

    ' 产品 -> 组
    For r = 2 To lastR
        p = Trim(CStr(ws.Cells(r, 1).Value))
        g = Trim(CStr(ws.Cells(r, 2).Value))
        If p <> "" And g <> "" And Not dictPG.Exists(p) Then dictPG.Add p, g
    Next r

    ' 组顺序1：汇总表模板中的组（保留无产品的组）
    Dim ord() As String
    ReDim ord(0 To 31)
    n = 0
    For r = 4 To 200
        g = Trim(CStr(wsSum.Cells(r, 1).Value))
        If g <> "" And g <> "总计" Then AddGroup seen, ord, n, g
    Next r

    ' 组顺序2：4号表新增的组
    For r = 2 To lastR
        g = Trim(CStr(ws.Cells(r, 2).Value))
        AddGroup seen, ord, n, g
    Next r
    If n > 0 Then ReDim Preserve ord(0 To n - 1) Else ReDim ord(0 To -1)
    groups = ord

    ' 产品列表（4号表顺序）
    Dim ordP() As String
    ReDim ordP(0 To dictPG.Count - 1)
    n = 0
    For r = 2 To lastR
        p = Trim(CStr(ws.Cells(r, 1).Value))
        If p <> "" And dictPG.Exists(p) Then
            ordP(n) = p
            n = n + 1
        End If
    Next r
    products = ordP

    ' 索引字典
    Set dictGI = CreateObject("Scripting.Dictionary")
    Set dictPI = CreateObject("Scripting.Dictionary")
    Dim i As Long
    For i = 0 To UBound(groups): dictGI.Add groups(i), i: Next i
    For i = 0 To UBound(products): dictPI.Add products(i), i: Next i
End Sub

Private Sub AddGroup(seen As Object, ByRef ord() As String, ByRef n As Long, ByVal g As String)
    If g = "" Then Exit Sub
    If seen.Exists(g) Then Exit Sub
    seen.Add g, 1
    If n > UBound(ord) Then ReDim Preserve ord(0 To n + 16)
    ord(n) = g
    n = n + 1
End Sub

'=============================================================
' 8.单价 -> 单价表（向前补齐）
'=============================================================
Private Sub LoadPrice(wb As Workbook, msgs As Collection)
    Dim ws As Worksheet
    Set ws = wb.Sheets(S_PRICE)
    Set dictPrice = CreateObject("Scripting.Dictionary")

    Dim lastR As Long, lastC As Long
    lastR = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    lastC = ws.Cells(2, ws.Columns.Count).End(xlToLeft).Column

    Dim r As Long, c As Long
    For r = 3 To lastR
        Dim p As String
        p = Trim(CStr(ws.Cells(r, 1).Value))
        If p = "" Then GoTo nextProd
        Dim lastV As Double
        Dim hasLast As Boolean
        hasLast = False
        For c = 2 To lastC
            If IsDate(ws.Cells(2, c).Value) Then
                Dim d As Long
                d = Int(CDate(ws.Cells(2, c).Value))
                If d >= Int(mStart) And d <= Int(mEnd) Then
                    Dim v As Variant
                    v = ws.Cells(r, c).Value
                    If IsNumeric(v) Then
                        lastV = CDbl(v)
                        hasLast = True
                        dictPrice(p & Chr(1) & CStr(d)) = lastV
                    ElseIf hasLast Then
                        dictPrice(p & Chr(1) & CStr(d)) = lastV
                    End If
                End If
            End If
        Next c
nextProd:
    Next r

    ' 检查缺单价的产品
    Dim pi As Long, di As Long
    Dim anyPr As Boolean
    For pi = 0 To UBound(products)
        anyPr = False
        For di = 0 To mDays - 1
            If dictPrice.Exists(products(pi) & Chr(1) & CStr(Int(mStart) + di)) Then
                anyPr = True
                Exit For
            End If
        Next di
        If Not anyPr Then msgs.Add "8.单价 缺少产品 " & products(pi) & " 整月单价"
    Next pi
End Sub

'=============================================================
' 6.PLAN -> Plan in / Plan out
'=============================================================
Private Sub LoadPlan(wb As Workbook, msgs As Collection)
    Dim ws As Worksheet
    Set ws = wb.Sheets(S_PLAN)
    Set dictPlanIn = CreateObject("Scripting.Dictionary")
    Set dictPlanOut = CreateObject("Scripting.Dictionary")

    Dim lastR As Long, lastC As Long
    lastR = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    lastC = ws.Cells(1, ws.Columns.Count).End(xlToLeft).Column

    ' 日期列（表头第1行）
    Dim colD() As Long
    ReDim colD(0 To lastC)
    Dim c As Long, dc As Long
    dc = 0
    For c = 1 To lastC
        If IsDate(ws.Cells(1, c).Value) Then
            colD(dc) = c
            dc = dc + 1
        End If
    Next c

    Dim curP As String
    curP = ""
    Dim hasP As Object
    Set hasP = CreateObject("Scripting.Dictionary")

    Dim r As Long
    For r = 2 To lastR
        Dim a As String
        a = Trim(CStr(ws.Cells(r, 1).Value))
        If a <> "" Then
            curP = a
            If Not hasP.Exists(a) Then hasP.Add a, 1
        End If
        Dim b As String
        b = LCase(Trim(CStr(ws.Cells(r, 2).Value)))
        Dim tgt As Object
        If b = "plan in" Then
            Set tgt = dictPlanIn
        ElseIf b = "plan out" Then
            Set tgt = dictPlanOut
        Else
            GoTo nextR2
        End If
        Dim i As Long
        For i = 0 To dc - 1
            Dim d As Long
            d = Int(CDate(ws.Cells(1, colD(i)).Value))
            If d >= Int(mStart) And d <= Int(mEnd) Then
                Dim v As Variant
                v = ws.Cells(r, colD(i)).Value
                If IsNumeric(v) Then tgt(curP & Chr(1) & CStr(d)) = CDbl(v)
            End If
        Next i
nextR2:
    Next r

    ' 检查缺计划的产品
    Dim pi As Long
    For pi = 0 To UBound(products)
        If Not hasP.Exists(products(pi)) Then
            msgs.Add "6.PLAN 缺少产品 " & products(pi) & " 的计划行"
        End If
    Next pi
End Sub

'=============================================================
' 10.异常 -> 在内存中调整 Plan
'=============================================================
Private Sub ApplyExceptions(wb As Workbook, msgs As Collection)
    Dim ws As Worksheet
    Set ws = wb.Sheets(S_EXC)
    Dim lastR As Long
    lastR = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    If lastR < 2 Then Exit Sub

    Dim r As Long
    For r = 2 To lastR
        Dim p As String
        p = Trim(CStr(ws.Cells(r, 1).Value))
        If p = "" Then GoTo nextE
        Dim q As Double
        q = 0
        If IsNumeric(ws.Cells(r, 2).Value) Then q = CDbl(ws.Cells(r, 2).Value)
        Dim et As String
        et = LCase(Trim(CStr(ws.Cells(r, 3).Value)))
        Dim scope As String
        scope = LCase(Trim(CStr(ws.Cells(r, 4).Value)))
        If Not IsDate(ws.Cells(r, 5).Value) Then
            msgs.Add "异常表第" & r & "行 old_SOD 不是日期，已跳过"
            GoTo nextE
        End If
        Dim oldD As Long, newD As Long
        oldD = Int(CDate(ws.Cells(r, 5).Value))
        newD = oldD
        If IsDate(ws.Cells(r, 6).Value) Then newD = Int(CDate(ws.Cells(r, 6).Value))

        Dim tgt As Object
        If InStr(scope, "out") > 0 And InStr(scope, "in") = 0 Then
            Set tgt = dictPlanOut
        ElseIf InStr(scope, "in") > 0 Then
            Set tgt = dictPlanIn
        Else
            msgs.Add "异常表第" & r & "行 影响范围无效：" & ws.Cells(r, 4).Value
            GoTo nextE
        End If

        Dim k As String
        If et = "delay" Then
            k = p & Chr(1) & CStr(oldD)
            If tgt.Exists(k) Then tgt(k) = CDbl(tgt(k)) - q Else tgt.Add k, -q
            k = p & Chr(1) & CStr(newD)
            If tgt.Exists(k) Then tgt(k) = CDbl(tgt(k)) + q Else tgt.Add k, q
        ElseIf et = "scrapped" Then
            k = p & Chr(1) & CStr(oldD)
            If tgt.Exists(k) Then tgt(k) = CDbl(tgt(k)) - q Else tgt.Add k, -q
        Else
            msgs.Add "异常表第" & r & "行 异常类型无效：" & ws.Cells(r, 3).Value
        End If
nextE:
    Next r
End Sub

'=============================================================
' 5.筛选条件 + 9.原始数据 -> 入料量/出货量
'=============================================================
Private Sub LoadFiltersAndRaw(wb As Workbook, msgs As Collection)
    Dim wsF As Worksheet
    Set wsF = wb.Sheets(S_FILTER)
    Dim wsR As Worksheet
    Set wsR = wb.Sheets(S_RAW)

    ' --- 筛选条件 ---
    Dim fLastR As Long, fLastC As Long
    fLastR = wsF.Cells(wsF.Rows.Count, 1).End(xlUp).Row
    fLastC = wsF.Cells(1, wsF.Columns.Count).End(xlToLeft).Column

    Dim fH() As String
    ReDim fH(1 To fLastC)
    Dim fc As Long
    For fc = 1 To fLastC
        fH(fc) = LCase(Trim(CStr(wsF.Cells(1, fc).Value)))
    Next fc
    Dim typeCol As Long
    typeCol = 0
    For fc = 1 To fLastC
        If fH(fc) = "类型" Then typeCol = fc
    Next fc
    If typeCol = 0 Then
        msgs.Add "5.筛选条件缺少“类型”列！"
        Exit Sub
    End If

    Dim inRows As Collection
    Dim outRows As Collection
    Set inRows = New Collection
    Set outRows = New Collection
    Dim r As Long
    For r = 2 To fLastR
        Dim tp As String
        tp = Trim(CStr(wsF.Cells(r, typeCol).Value))
        If tp = "" Then GoTo nextF
        Dim pairs As Collection
        Set pairs = New Collection
        For fc = 1 To fLastC
            If fc <> typeCol Then
                Dim fv As String
                fv = Trim(CStr(wsF.Cells(r, fc).Value))
                If fv <> "" Then pairs.Add fH(fc) & Chr(2) & fv
            End If
        Next fc
        If pairs.Count > 0 Then
            If tp = "入料" Then inRows.Add pairs
            If tp = "出货" Then outRows.Add pairs
        End If
nextF:
    Next r
    If inRows.Count = 0 And outRows.Count = 0 Then
        msgs.Add "5.筛选条件为空，无法匹配原始数据！"
        Exit Sub
    End If

    ' --- 原始数据表头 ---
    Dim rLastC As Long
    rLastC = wsR.Cells(1, wsR.Columns.Count).End(xlToLeft).Column
    Dim hIdx As Object
    Set hIdx = CreateObject("Scripting.Dictionary")
    Dim hn As String
    For fc = 1 To rLastC
        hn = LCase(Trim(CStr(wsR.Cells(1, fc).Value)))
        If hn <> "" And Not hIdx.Exists(hn) Then hIdx.Add hn, fc
    Next fc

    If Not hIdx.Exists("product_name") Or Not hIdx.Exists("component_qty") Or Not hIdx.Exists("last_updated_time") Then
        msgs.Add "9.原始数据缺少必要列 (product_name/component_qty/last_updated_time)！"
        Exit Sub
    End If
    If Not hIdx.Exists("activity") Then
        msgs.Add "9.原始数据缺少 activity 列，筛选将无法匹配！"
        Exit Sub
    End If

    ' 缺少字段检查
    Dim missingF As Object
    Set missingF = CreateObject("Scripting.Dictionary")
    Dim i1 As Long, j1 As Long
    Dim pr As Collection
    Dim s1 As String, sp1 As Long, fn1 As String
    For i1 = 1 To inRows.Count
        Set pr = inRows(i1)
        For j1 = 1 To pr.Count
            s1 = pr(j1)
            sp1 = InStr(s1, Chr(2))
            fn1 = Left(s1, sp1 - 1)
            If Not hIdx.Exists(fn1) And Not missingF.Exists(fn1) Then missingF.Add fn1, 1
        Next j1
    Next i1
    For i1 = 1 To outRows.Count
        Set pr = outRows(i1)
        For j1 = 1 To pr.Count
            s1 = pr(j1)
            sp1 = InStr(s1, Chr(2))
            fn1 = Left(s1, sp1 - 1)
            If Not hIdx.Exists(fn1) And Not missingF.Exists(fn1) Then missingF.Add fn1, 1
        Next j1
    Next i1
    Dim mk As Variant
    For Each mk In missingF.Keys
        msgs.Add "原始数据缺少筛选字段列: " & mk
    Next mk

    Set dictRecvQty = CreateObject("Scripting.Dictionary")
    Set dictShipQty = CreateObject("Scripting.Dictionary")

    Dim lastRow As Long
    lastRow = wsR.Cells(wsR.Rows.Count, 1).End(xlUp).Row
    If lastRow < 2 Then
        msgs.Add "9.原始数据为空（仅表头）"
        Exit Sub
    End If

    Dim qtyCol As Long, pnameCol As Long, timeCol As Long
    qtyCol = hIdx("component_qty")
    pnameCol = hIdx("product_name")
    timeCol = hIdx("last_updated_time")

    ' 整表读入内存，加速计算
    Dim data As Variant
    data = wsR.Range(wsR.Cells(1, 1), wsR.Cells(lastRow, rLastC)).Value

    Dim matchedIn As Long, matchedOut As Long, ignored As Long
    Dim rr As Long
    For rr = 2 To lastRow
        Dim dv As Variant
        dv = data(rr, timeCol)
        If Not IsDate(dv) Then
            ignored = ignored + 1
            GoTo nextR
        End If
        Dim d As Long
        d = Int(CDate(dv))
        If d < Int(mStart) Or d > Int(mEnd) Then GoTo nextR

        Dim q As Double
        q = 0
        If IsNumeric(data(rr, qtyCol)) Then q = CDbl(data(rr, qtyCol))

        Dim p As String
        p = Trim(CStr(data(rr, pnameCol)))
        If p = "" Then GoTo nextR

        Dim t As String
        t = ""
        If MatchCondArray(data, rr, hIdx, inRows) Then
            t = "入料"
        ElseIf MatchCondArray(data, rr, hIdx, outRows) Then
            t = "出货"
        End If

        If t = "" Then
            ignored = ignored + 1
        Else
            Dim k As String
            k = p & Chr(1) & CStr(d)
            If t = "入料" Then
                matchedIn = matchedIn + 1
                If dictRecvQty.Exists(k) Then dictRecvQty(k) = CDbl(dictRecvQty(k)) + q Else dictRecvQty.Add k, q
            Else
                matchedOut = matchedOut + 1
                If dictShipQty.Exists(k) Then dictShipQty(k) = CDbl(dictShipQty(k)) + q Else dictShipQty.Add k, q
            End If
        End If
nextR:
    Next rr
    msgs.Add "原始数据：入料匹配 " & matchedIn & " 行，出货匹配 " & matchedOut & " 行，未匹配/忽略 " & ignored & " 行。"
End Sub

Private Function MatchCondArray(data As Variant, ByVal rr As Long, hIdx As Object, rows As Collection) As Boolean
    Dim i As Long, j As Long
    Dim pr As Collection
    For i = 1 To rows.Count
        Set pr = rows(i)
        Dim ok As Boolean
        ok = True
        For j = 1 To pr.Count
            Dim s As String
            s = pr(j)
            Dim sp As Long
            sp = InStr(s, Chr(2))
            Dim fn As String
            fn = Left(s, sp - 1)
            Dim fv As String
            fv = Mid(s, sp + 1)
            If Not hIdx.Exists(fn) Then
                ok = False
                Exit For
            End If
            Dim ci As Long
            ci = hIdx(fn)
            If LCase(Trim(CStr(data(rr, ci)))) <> LCase(fv) Then
                ok = False
                Exit For
            End If
        Next j
        If ok Then
            MatchCondArray = True
            Exit Function
        End If
    Next i
End Function

'=============================================================
' 计算 + 写入 1/2/3 表
'=============================================================
Private Sub ComputeAndWriteSheets(wb As Workbook, msgs As Collection)
    Dim nG As Long, nP As Long
    nG = UBound(groups) + 1
    nP = UBound(products) + 1

    Dim planInA() As Double, amtInA() As Double, cumInA() As Double, pctInA() As Double
    Dim planOutA() As Double, amtOutA() As Double, cumOutA() As Double, pctOutA() As Double
    Dim planInM() As Double, amtInM() As Double, planOutM() As Double, amtOutM() As Double
    Dim prodQtyA() As Double, shipQtyA() As Double, shipQtyG() As Double

    ReDim planInA(0 To nG - 1, 0 To mDays - 1)
    ReDim amtInA(0 To nG - 1, 0 To mDays - 1)
    ReDim cumInA(0 To nG - 1, 0 To mDays - 1)
    ReDim pctInA(0 To nG - 1, 0 To mDays - 1)
    ReDim planOutA(0 To nG - 1, 0 To mDays - 1)
    ReDim amtOutA(0 To nG - 1, 0 To mDays - 1)
    ReDim cumOutA(0 To nG - 1, 0 To mDays - 1)
    ReDim pctOutA(0 To nG - 1, 0 To mDays - 1)
    ReDim planInM(0 To nG - 1)
    ReDim amtInM(0 To nG - 1)
    ReDim planOutM(0 To nG - 1)
    ReDim amtOutM(0 To nG - 1)
    ReDim prodQtyA(0 To nP - 1, 0 To mDays - 1)
    ReDim shipQtyA(0 To nP - 1, 0 To mDays - 1)
    ReDim shipQtyG(0 To nG - 1, 0 To mDays - 1)

    ' 入料量/出货量（按产品）
    Dim p As Long, d As Long, dd As Long, kk As String, pn As String
    For p = 0 To nP - 1
        pn = products(p)
        For d = 0 To mDays - 1
            dd = Int(mStart) + d
            kk = pn & Chr(1) & CStr(dd)
            If dictRecvQty.Exists(kk) Then prodQtyA(p, d) = CDbl(dictRecvQty(kk))
            If dictShipQty.Exists(kk) Then shipQtyA(p, d) = CDbl(dictShipQty(kk))
        Next d
    Next p

    Dim g As Long
    Dim pr As Double, pin As Double, pout As Double
    For g = 0 To nG - 1
        For d = 0 To mDays - 1
            dd = Int(mStart) + d
            For p = 0 To nP - 1
                pn = products(p)
                If dictPG(pn) = groups(g) Then
                    kk = pn & Chr(1) & CStr(dd)
                    If dictPrice.Exists(kk) Then pr = CDbl(dictPrice(kk)) Else pr = 0
                    If dictPlanIn.Exists(kk) Then pin = CDbl(dictPlanIn(kk)) Else pin = 0
                    If dictPlanOut.Exists(kk) Then pout = CDbl(dictPlanOut(kk)) Else pout = 0
                    planInA(g, d) = planInA(g, d) + pin * pr
                    planOutA(g, d) = planOutA(g, d) + pout * pr
                    amtInA(g, d) = amtInA(g, d) + prodQtyA(p, d) * pr
                    amtOutA(g, d) = amtOutA(g, d) + shipQtyA(p, d) * pr
                    shipQtyG(g, d) = shipQtyG(g, d) + shipQtyA(p, d)
                End If
            Next p
            If d = 0 Then
                cumInA(g, d) = amtInA(g, d)
                cumOutA(g, d) = amtOutA(g, d)
            Else
                cumInA(g, d) = cumInA(g, d - 1) + amtInA(g, d)
                cumOutA(g, d) = cumOutA(g, d - 1) + amtOutA(g, d)
            End If
            If planInA(g, d) <> 0 Then pctInA(g, d) = amtInA(g, d) / planInA(g, d)
            If planOutA(g, d) <> 0 Then pctOutA(g, d) = amtOutA(g, d) / planOutA(g, d)
            planInM(g) = planInM(g) + planInA(g, d)
            planOutM(g) = planOutM(g) + planOutA(g, d)
            amtInM(g) = amtInM(g) + amtInA(g, d)
            amtOutM(g) = amtOutM(g) + amtOutA(g, d)
        Next d
    Next g

    WriteRecvSheet wb.Sheets(S_RECV), planInA, prodQtyA, amtInA, cumInA, pctInA, planInM, amtInM
    WriteShipSheet wb.Sheets(S_SHIP), planOutA, shipQtyG, amtOutA, cumOutA, pctOutA, planOutM, amtOutM
    WriteSummarySheet wb.Sheets(S_SUMMARY), pctInA, pctOutA, amtInA, planInA, amtOutA, planOutA, amtInM, planInM, amtOutM, planOutM
End Sub

'=============================================================
' 2.入料
'=============================================================
Private Sub WriteRecvSheet(ws As Worksheet, planInA() As Double, prodQtyA() As Double, amtInA() As Double, cumInA() As Double, pctInA() As Double, planInM() As Double, amtInM() As Double)
    ws.Cells.Clear
    Dim nG As Long, nP As Long
    nG = UBound(groups) + 1
    nP = UBound(products) + 1
    Dim r As Long, g As Long, p As Long, d As Long
    Dim startC As Long
    startC = 2

    ' FCST PLAN(按组)
    r = 1
    ws.Cells(r, 1).Value = "FCST PLAN"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = 2
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(planInA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' 入料量(按产品)
    ws.Cells(r, 1).Value = "入料量"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "Product_name\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For p = 0 To nP - 1
        r = r + 1
        ws.Cells(r, 1).Value = products(p)
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = prodQtyA(p, d)
            ws.Cells(r, startC + d).NumberFormat = "#,##0"
        Next d
    Next p
    r = r + 1

    ' 入料金额(按组)
    ws.Cells(r, 1).Value = "入料金额"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(amtInA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' 入料累计金额
    ws.Cells(r, 1).Value = "入料累计金额"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(cumInA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' receiving%(按组，含月合计列)
    ws.Cells(r, 1).Value = "receiving%"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 1
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = pctInA(g, d)
            ws.Cells(r, startC + d).NumberFormat = "0.0%"
        Next d
        If planInM(g) <> 0 Then ws.Cells(r, startC + mDays).Value = amtInM(g) / planInM(g)
        ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
        ws.Cells(r, startC + mDays).Font.Bold = True
    Next g

    ' 列宽
    ws.Columns(1).ColumnWidth = 16
    ws.Columns(2).ColumnWidth = 5.5
    ws.Columns(2 + mDays).ColumnWidth = 9
End Sub

'=============================================================
' 3.出货
'=============================================================
Private Sub WriteShipSheet(ws As Worksheet, planOutA() As Double, shipQtyG() As Double, amtOutA() As Double, cumOutA() As Double, pctOutA() As Double, planOutM() As Double, amtOutM() As Double)
    ws.Cells.Clear
    Dim nG As Long
    nG = UBound(groups) + 1
    Dim r As Long, g As Long, d As Long
    Dim startC As Long
    startC = 2

    ' FCST PLAN
    r = 1
    ws.Cells(r, 1).Value = "FCST PLAN"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = 2
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(planOutA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' 出货量(按组)
    ws.Cells(r, 1).Value = "出货量"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = shipQtyG(g, d)
            ws.Cells(r, startC + d).NumberFormat = "#,##0"
        Next d
    Next g
    r = r + 1

    ' 出货金额
    ws.Cells(r, 1).Value = "出货金额"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(amtOutA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' 出货累计金额
    ws.Cells(r, 1).Value = "出货累计金额"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 0
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = Round(cumOutA(g, d), 2)
            ws.Cells(r, startC + d).NumberFormat = "#,##0.00"
        Next d
    Next g
    r = r + 1

    ' biling%
    ws.Cells(r, 1).Value = "biling%"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 1).Font.Size = 12
    r = r + 1
    ws.Cells(r, 1).Value = "PKG Group\日期"
    ws.Cells(r, 1).Font.Bold = True
    WriteDateHeader ws, r, startC, 1
    For g = 0 To nG - 1
        r = r + 1
        ws.Cells(r, 1).Value = groups(g)
        ws.Cells(r, 1).Font.Bold = True
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = pctOutA(g, d)
            ws.Cells(r, startC + d).NumberFormat = "0.0%"
        Next d
        If planOutM(g) <> 0 Then ws.Cells(r, startC + mDays).Value = amtOutM(g) / planOutM(g)
        ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
        ws.Cells(r, startC + mDays).Font.Bold = True
    Next g

    ws.Columns(1).ColumnWidth = 16
    ws.Columns(2).ColumnWidth = 5.5
    ws.Columns(2 + mDays).ColumnWidth = 9
End Sub

'=============================================================
' 1.汇总
'=============================================================
Private Sub WriteSummarySheet(ws As Worksheet, pctInA() As Double, pctOutA() As Double, amtInA() As Double, planInA() As Double, amtOutA() As Double, planOutA() As Double, amtInM() As Double, planInM() As Double, amtOutM() As Double, planOutM() As Double)
    ws.Cells.Clear
    ws.Cells(1, 1).Value = mStart
    ws.Cells(1, 1).NumberFormat = "yyyy-mm"

    Dim nG As Long
    nG = UBound(groups) + 1
    Dim r As Long, d As Long, g As Long
    Dim startC As Long
    startC = 3

    r = 3
    ws.Cells(r, 1).Value = "PKG Group"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 2).Value = "item"
    ws.Cells(r, 2).Font.Bold = True
    WriteDateHeader ws, r, startC, 1

    ' 总计计算
    Dim totAmtIn() As Double, totPlanIn() As Double, totAmtOut() As Double, totPlanOut() As Double
    ReDim totAmtIn(0 To mDays - 1)
    ReDim totPlanIn(0 To mDays - 1)
    ReDim totAmtOut(0 To mDays - 1)
    ReDim totPlanOut(0 To mDays - 1)
    Dim totAmtInM As Double, totPlanInM As Double, totAmtOutM As Double, totPlanOutM As Double
    Dim gi As Long
    For gi = 0 To nG - 1
        For d = 0 To mDays - 1
            totAmtIn(d) = totAmtIn(d) + amtInA(gi, d)
            totPlanIn(d) = totPlanIn(d) + planInA(gi, d)
            totAmtOut(d) = totAmtOut(d) + amtOutA(gi, d)
            totPlanOut(d) = totPlanOut(d) + planOutA(gi, d)
        Next d
        totAmtInM = totAmtInM + amtInM(gi)
        totPlanInM = totPlanInM + planInM(gi)
        totAmtOutM = totAmtOutM + amtOutM(gi)
        totPlanOutM = totPlanOutM + planOutM(gi)
    Next gi

    r = 4
    For gi = 0 To nG - 1
        ' R%
        ws.Cells(r, 1).Value = groups(gi)
        ws.Cells(r, 1).Font.Bold = True
        ws.Cells(r, 2).Value = "R%"
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = pctInA(gi, d)
            ws.Cells(r, startC + d).NumberFormat = "0.0%"
        Next d
        If planInM(gi) <> 0 Then ws.Cells(r, startC + mDays).Value = amtInM(gi) / planInM(gi)
        ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
        ws.Cells(r, startC + mDays).Font.Bold = True
        ' B%
        r = r + 1
        ws.Cells(r, 2).Value = "B%"
        For d = 0 To mDays - 1
            ws.Cells(r, startC + d).Value = pctOutA(gi, d)
            ws.Cells(r, startC + d).NumberFormat = "0.0%"
        Next d
        If planOutM(gi) <> 0 Then ws.Cells(r, startC + mDays).Value = amtOutM(gi) / planOutM(gi)
        ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
        ws.Cells(r, startC + mDays).Font.Bold = True
        ws.Range(ws.Cells(r - 1, 1), ws.Cells(r, 1)).Merge
        r = r + 1
    Next gi

    ' 总计
    ws.Cells(r, 1).Value = "总计"
    ws.Cells(r, 1).Font.Bold = True
    ws.Cells(r, 2).Value = "R%"
    ws.Cells(r, 2).Font.Bold = True
    For d = 0 To mDays - 1
        If totPlanIn(d) <> 0 Then ws.Cells(r, startC + d).Value = totAmtIn(d) / totPlanIn(d)
        ws.Cells(r, startC + d).NumberFormat = "0.0%"
    Next d
    If totPlanInM <> 0 Then ws.Cells(r, startC + mDays).Value = totAmtInM / totPlanInM
    ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
    ws.Cells(r, startC + mDays).Font.Bold = True
    r = r + 1
    ws.Cells(r, 2).Value = "B%"
    ws.Cells(r, 2).Font.Bold = True
    For d = 0 To mDays - 1
        If totPlanOut(d) <> 0 Then ws.Cells(r, startC + d).Value = totAmtOut(d) / totPlanOut(d)
        ws.Cells(r, startC + d).NumberFormat = "0.0%"
    Next d
    If totPlanOutM <> 0 Then ws.Cells(r, startC + mDays).Value = totAmtOutM / totPlanOutM
    ws.Cells(r, startC + mDays).NumberFormat = "0.0%"
    ws.Cells(r, startC + mDays).Font.Bold = True
    ws.Range(ws.Cells(r - 1, 1), ws.Cells(r, 1)).Merge
    ws.Cells(r - 1, 1).Font.Bold = True

    ws.Columns(1).ColumnWidth = 14
    ws.Columns(2).ColumnWidth = 7
    ws.Columns(3).ColumnWidth = 5.5
    ws.Columns(3 + mDays).ColumnWidth = 9
End Sub

'=============================================================
' 辅助：日期表头
'=============================================================
Private Sub WriteDateHeader(ws As Worksheet, ByVal r As Long, ByVal startC As Long, ByVal withTotal As Long)
    Dim d As Long
    For d = 0 To mDays - 1
        ws.Cells(r, startC + d).Value = mStart + d
        ws.Cells(r, startC + d).NumberFormat = "m/d"
        ws.Cells(r, startC + d).Font.Bold = True
        ws.Cells(r, startC + d).Interior.Color = RGB(242, 242, 242)
    Next d
    If withTotal = 1 Then
        ws.Cells(r, startC + mDays).Value = "月合计"
        ws.Cells(r, startC + mDays).Font.Bold = True
        ws.Cells(r, startC + mDays).Interior.Color = RGB(255, 235, 156)
    End If
End Sub

'=============================================================
' 辅助：为输入表补日期列
'=============================================================
Private Sub EnsureInputDateHeaders(ws As Worksheet, ByVal headerRow As Long, ByVal firstCol As Long)
    Dim lastC As Long
    lastC = ws.Cells(headerRow, ws.Columns.Count).End(xlToLeft).Column
    Dim existing As Object
    Set existing = CreateObject("Scripting.Dictionary")
    Dim c As Long
    For c = firstCol To lastC
        If IsDate(ws.Cells(headerRow, c).Value) Then
            existing(Int(CDate(ws.Cells(headerRow, c).Value))) = 1
        End If
    Next c
    Dim startCol As Long
    startCol = lastC + 1
    Dim d As Long
    For d = Int(mStart) To Int(mEnd)
        If Not existing.Exists(d) Then
            ws.Cells(headerRow, startCol).Value = CDate(d)
            ws.Cells(headerRow, startCol).NumberFormat = "m/d"
            startCol = startCol + 1
        End If
    Next d
End Sub

'=============================================================
' 辅助：解析 yyyy-mm
'=============================================================
Private Function ParseYearMonth(ByVal s As String) As Date
    s = Replace(Replace(Trim(s), "/", "-"), ".", "-")
    If InStr(s, "-") = 0 Then Exit Function
    Dim arr As Variant
    arr = Split(s, "-")
    If UBound(arr) < 1 Then Exit Function
    Dim y As Long, mo As Long
    y = Val(arr(0))
    mo = Val(arr(1))
    If y < 100 Then y = y + 2000
    If mo >= 1 And mo <= 12 And y >= 1900 Then
        ParseYearMonth = DateSerial(y, mo, 1)
    End If
End Function
