* Ejemplo Visual FoxPro para consumir facturas OCR desde MySQL staging.
* Ajustar DSN/usuario/clave desde parametros del sistema, no hardcodear en produccion.

#DEFINE CRLF CHR(13) + CHR(10)

FUNCTION OcrSqlConnect(tcDsn, tcUser, tcPassword)
    LOCAL lcConn, lnConn
    lcConn = "DSN=" + tcDsn + ";UID=" + tcUser + ";PWD=" + tcPassword + ";"
    lnConn = SQLSTRINGCONNECT(lcConn)
    IF lnConn < 0
        AERROR(laErr)
        MESSAGEBOX("No se pudo conectar a MySQL OCR:" + CRLF + laErr[2], 16, "Facturas OCR")
    ENDIF
    RETURN lnConn
ENDFUNC


FUNCTION OcrPendientes(lnConn, tcCursor)
    LOCAL lcCursor, lcSql, lnOk
    lcCursor = IIF(EMPTY(tcCursor), "curOcrPendientes", tcCursor)
    lcSql = ;
        "SELECT id, fecha_proceso, proveedor_codigo, proveedor_nombre, " + ;
        "emisor_razon_social, emisor_cuit, comprobante, fecha_emision, total, " + ;
        "percepciones_iibb, " + ;
        "cuenta_contable_sugerida, cuenta_descripcion_sugerida, score_sugerencia, " + ;
        "requiere_revision, observaciones " + ;
        "FROM vw_facturas_ocr_pendientes " + ;
        "ORDER BY requiere_revision DESC, fecha_proceso"

    lnOk = SQLEXEC(lnConn, lcSql, lcCursor)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudieron leer pendientes OCR"
        RETURN .F.
    ENDIF
    RETURN .T.
ENDFUNC


FUNCTION OcrDetalle(lnConn, tnFacturaId, tcCursor)
    LOCAL lcCursor, lcSql, lnOk
    lcCursor = IIF(EMPTY(tcCursor), "curOcrDetalle", tcCursor)
    lcSql = ;
        "SELECT id, factura_id, linea, descripcion_factura, cantidad, precio_unitario, subtotal, " + ;
        "cuenta_contable, cuenta_descripcion, origen_sugerencia, score_sugerencia, " + ;
        "requiere_confirmacion, confirmada " + ;
        "FROM vw_facturas_ocr_detalle " + ;
        "WHERE factura_id = ?tnFacturaId " + ;
        "ORDER BY linea"

    lnOk = SQLEXEC(lnConn, lcSql, lcCursor)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudo leer detalle OCR"
        RETURN .F.
    ENDIF
    RETURN .T.
ENDFUNC


FUNCTION OcrPercepcionesIibb(lnConn, tnFacturaId, tcCursor)
    LOCAL lcCursor, lcSql, lnOk
    lcCursor = IIF(EMPTY(tcCursor), "curOcrPercepcionesIibb", tcCursor)
    lcSql = ;
        "SELECT id, factura_id, linea, jurisdiccion, codjur, importe " + ;
        "FROM vw_facturas_ocr_percepciones_iibb " + ;
        "WHERE factura_id = ?tnFacturaId " + ;
        "ORDER BY linea"

    lnOk = SQLEXEC(lnConn, lcSql, lcCursor)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudieron leer percepciones DGR/IIBB"
        RETURN .F.
    ENDIF
    RETURN .T.
ENDFUNC


PROCEDURE OcrMostrarFactura(lnConn, tnFacturaId)
    LOCAL llDetalle, llPercepciones

    llDetalle = OcrDetalle(lnConn, tnFacturaId, "curOcrDetalle")
    llPercepciones = OcrPercepcionesIibb(lnConn, tnFacturaId, "curOcrPercepcionesIibb")

    IF llDetalle AND USED("curOcrDetalle")
        SELECT curOcrDetalle
        BROWSE NOWAIT NORMAL TITLE "Detalle factura OCR"
    ENDIF

    IF llPercepciones
        DO OcrMostrarPercepcionesIibb WITH "curOcrPercepcionesIibb"
    ENDIF
ENDPROC


PROCEDURE OcrMostrarPercepcionesIibb(tcCursor)
    LOCAL lcCursor, lcMsg, lnTotal
    lcCursor = IIF(EMPTY(tcCursor), "curOcrPercepcionesIibb", tcCursor)

    IF !USED(lcCursor)
        MESSAGEBOX("No hay un cursor de percepciones DGR/IIBB abierto.", 48, "Facturas OCR")
        RETURN
    ENDIF

    SELECT (lcCursor)
    IF RECCOUNT() = 0
        MESSAGEBOX("La factura no tiene percepciones DGR/IIBB detectadas.", 64, "Facturas OCR")
        RETURN
    ENDIF

    lcMsg = ""
    lnTotal = 0
    SCAN
        lnTotal = lnTotal + importe
        lcMsg = lcMsg + ;
            ALLTRIM(jurisdiccion) + " (" + ALLTRIM(codjur) + "): $" + ;
            ALLTRIM(TRANSFORM(importe, "999,999,999.99")) + CRLF
    ENDSCAN

    lcMsg = lcMsg + CRLF + "Total DGR/IIBB: $" + ;
        ALLTRIM(TRANSFORM(lnTotal, "999,999,999.99"))

    MESSAGEBOX(lcMsg, 64, "Percepciones DGR/IIBB")
    GO TOP
    BROWSE NOWAIT NORMAL TITLE "Percepciones DGR/IIBB"
ENDPROC


FUNCTION OcrConfirmarCuenta(lnConn, tnDetalleId, tcCuenta, tcDescripcion, tcUsuario, tcPatronTexto, tcPatronNormalizado)
    LOCAL lcSql, lnOk

    lcSql = ;
        "UPDATE facturas_ocr_detalle " + ;
        "SET cuenta_contable = ?tcCuenta, cuenta_descripcion = ?tcDescripcion, " + ;
        "requiere_confirmacion = 0, confirmada = 1, usuario_confirmacion = ?tcUsuario, " + ;
        "fecha_confirmacion = NOW() " + ;
        "WHERE id = ?tnDetalleId"
    lnOk = SQLEXEC(lnConn, lcSql)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudo confirmar cuenta"
        RETURN .F.
    ENDIF

    DO OcrEventoCuenta WITH lnConn, tnDetalleId, tcUsuario

    IF !EMPTY(tcCuenta)
        DO OcrGuardarRegla WITH lnConn, tnDetalleId, tcPatronTexto, tcPatronNormalizado
    ENDIF

    RETURN .T.
ENDFUNC


FUNCTION OcrGuardarRegla(lnConn, tnDetalleId, tcPatronTexto, tcPatronNormalizado)
    LOCAL lcSql, lnOk
    lcSql = ;
        "INSERT INTO factura_reglas_contables (" + ;
        "proveedor_codigo, proveedor_nombre, patron_texto, patron_normalizado, palabras_clave, " + ;
        "importe_min, importe_max, tolerancia_pct, periodicidad, cuenta_contable, cuenta_descripcion, " + ;
        "prioridad, confianza, veces_usada, veces_confirmada, origen, requiere_revision, activa, " + ;
        "primera_fecha, ultima_fecha, ultima_confirmacion, notas, created_at, updated_at" + ;
        ") " + ;
        "SELECT c.proveedor_codigo, c.proveedor_nombre, ?tcPatronTexto, ?tcPatronNormalizado, '', " + ;
        "0, 0, 0, 'sin_definir', d.cuenta_contable, d.cuenta_descripcion, " + ;
        "100, 100, 1, 1, 'manual', 0, 1, c.fecha_emision, c.fecha_emision, NOW(), " + ;
        "'Confirmada desde Visual FoxPro OCR', NOW(), NOW() " + ;
        "FROM facturas_ocr_detalle d " + ;
        "JOIN facturas_ocr_cabecera c ON c.id = d.factura_id " + ;
        "WHERE d.id = ?tnDetalleId " + ;
        "ON DUPLICATE KEY UPDATE " + ;
        "cuenta_contable = VALUES(cuenta_contable), cuenta_descripcion = VALUES(cuenta_descripcion), " + ;
        "confianza = 100, requiere_revision = 0, activa = 1, origen = 'manual', " + ;
        "veces_confirmada = veces_confirmada + 1, ultima_confirmacion = NOW(), updated_at = NOW()"

    lnOk = SQLEXEC(lnConn, lcSql)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudo guardar regla contable"
        RETURN .F.
    ENDIF
    RETURN .T.
ENDFUNC


FUNCTION OcrMarcarImportada(lnConn, tnFacturaId, tcUsuario, tcObservaciones)
    LOCAL lcSql, lnOk
    lcSql = ;
        "UPDATE facturas_ocr_cabecera " + ;
        "SET importada = 1, fecha_importacion = NOW(), usuario_importacion = ?tcUsuario, " + ;
        "observaciones_importacion = ?tcObservaciones " + ;
        "WHERE id = ?tnFacturaId"
    lnOk = SQLEXEC(lnConn, lcSql)
    IF lnOk < 0
        DO OcrShowSqlError WITH "No se pudo marcar factura OCR como importada"
        RETURN .F.
    ENDIF

    lcSql = ;
        "INSERT INTO facturas_ocr_eventos (factura_id, sha256, evento, detalle, usuario) " + ;
        "SELECT id, sha256, 'factura_importada', 'Factura importada en Visual FoxPro', ?tcUsuario " + ;
        "FROM facturas_ocr_cabecera WHERE id = ?tnFacturaId"
    =SQLEXEC(lnConn, lcSql)
    RETURN .T.
ENDFUNC


FUNCTION OcrEventoCuenta(lnConn, tnDetalleId, tcUsuario)
    LOCAL lcSql
    lcSql = ;
        "INSERT INTO facturas_ocr_eventos (factura_id, detalle_id, sha256, evento, detalle, usuario) " + ;
        "SELECT d.factura_id, d.id, d.sha256, 'cuenta_confirmada', " + ;
        "CONCAT('Cuenta confirmada: ', d.cuenta_contable, ' ', d.cuenta_descripcion), ?tcUsuario " + ;
        "FROM facturas_ocr_detalle d WHERE d.id = ?tnDetalleId"
    =SQLEXEC(lnConn, lcSql)
ENDFUNC


PROCEDURE OcrShowSqlError(tcMessage)
    LOCAL laErr[1]
    AERROR(laErr)
    MESSAGEBOX(tcMessage + CRLF + laErr[2], 16, "Facturas OCR")
ENDPROC
