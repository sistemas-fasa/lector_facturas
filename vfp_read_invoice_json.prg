* Lee facturas parseadas por n8n y marca cada .ready como .done.
* Ajustar lcOutputDir desde config/app.ini, variable de entorno o tabla de parametros.

LOCAL lcOutputDir, laReady[1], lnCount, lnI
lcOutputDir = GETENV("INVOICE_OUTPUT_DIR")
IF EMPTY(lcOutputDir)
    lcOutputDir = "/var/data/facturas_parseadas"
ENDIF

lnCount = ADIR(laReady, ADDBS(lcOutputDir) + "*.ready")
FOR lnI = 1 TO lnCount
    LOCAL lcReady, lcJson, lcDone, lcJsonText, loFactura
    lcReady = ADDBS(lcOutputDir) + laReady[lnI, 1]
    lcJson = FORCEEXT(lcReady, "json")
    lcDone = FORCEEXT(lcReady, "done")

    IF !FILE(lcJson)
        ? "Falta JSON para ready:", lcReady
        LOOP
    ENDIF

    lcJsonText = FILETOSTR(lcJson)
    * Si el runtime VFP no interpreta UTF-8 correctamente, convertir:
    * lcJsonText = STRCONV(lcJsonText, 11)

    loFactura = ParseJson(lcJsonText)
    IF VARTYPE(loFactura) = "O"
        ? "Fecha:", loFactura.comprobante.fecha_emision
        ? "Numero:", loFactura.comprobante.numero
        ? "CUIT emisor:", loFactura.emisor.cuit
        ? "Total:", loFactura.importes.total
    ELSE
        ? "No se pudo parsear:", lcJson
        LOOP
    ENDIF

    RENAME (lcReady) TO (lcDone)
ENDFOR

FUNCTION ParseJson(tcJson)
    * Placeholder: conectar aca la libreria JSON usada por el ERP.
    * Opciones habituales: nfJson, wwJsonSerializer o parser propio controlado.
    * Debe devolver un objeto con comprobante, emisor e importes.
    RETURN .NULL.
ENDFUNC
