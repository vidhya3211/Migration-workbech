       IDENTIFICATION DIVISION.
       PROGRAM-ID. PREMCALC.
      *----------------------------------------------------------------*
      * Monthly premium calculation - Level Term Assurance book        *
      * CHG0412 1998-03 age loading introduced                         *
      * CHG0977 2004-11 annual payment discount                        *
      *----------------------------------------------------------------*
       ENVIRONMENT DIVISION.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
           COPY POLREC.
       01  WS-CONSTANTS.
           05  WS-ADMIN-FEE           PIC 9(3)V99  VALUE 25.00.
           05  WS-SMOKER-LOADING      PIC 9V999    VALUE 1.500.
           05  WS-AGE-LOAD-THRESHOLD  PIC 99       VALUE 60.
           05  WS-AGE-LOAD-FACTOR     PIC 9V999    VALUE 1.250.
           05  WS-ANNUAL-DISC-RATE    PIC V999     VALUE .030.
       01  WS-WORK.
           05  WS-BASE-PREMIUM        PIC 9(7)V99  VALUE ZERO.
           05  WS-RISK-FACTOR         PIC 9V9999.
           05  WS-TERM-FACTOR         PIC 9V9999.
           05  WS-MONTHLY-PREMIUM     PIC 9(7)V99.
           05  WS-ANNUAL-PREMIUM      PIC 9(7)V99.
           05  WS-DISCOUNT            PIC 9(5)V99.
       PROCEDURE DIVISION.
       0000-MAIN.
           PERFORM 1000-INIT
           PERFORM 2000-RISK-FACTOR
           PERFORM 3000-CALC-PREMIUM
           PERFORM 4000-APPLY-DISCOUNT
           GOBACK.
       1000-INIT.
           MOVE 1 TO WS-RISK-FACTOR
           EXEC SQL
               SELECT BASE_RATE INTO :POL-BASE-RATE
                 FROM LTA_RATE_TABLE
                WHERE TERM_YEARS = :POL-TERM-YEARS
           END-EXEC.
       2000-RISK-FACTOR.
           IF POL-SMOKER
               MULTIPLY WS-SMOKER-LOADING BY WS-RISK-FACTOR
           END-IF
           IF POL-HOLDER-AGE >= WS-AGE-LOAD-THRESHOLD
               COMPUTE WS-RISK-FACTOR ROUNDED =
                   WS-RISK-FACTOR * WS-AGE-LOAD-FACTOR
           END-IF.
       3000-CALC-PREMIUM.
           EVALUATE POL-TERM-YEARS
               WHEN 1 THRU 10
                   MOVE 1.000 TO WS-TERM-FACTOR
               WHEN 11 THRU 20
                   MOVE 1.150 TO WS-TERM-FACTOR
               WHEN OTHER
                   MOVE 1.300 TO WS-TERM-FACTOR
           END-EVALUATE
           COMPUTE WS-BASE-PREMIUM ROUNDED =
               (POL-SUM-ASSURED / 1000) * POL-BASE-RATE
               * WS-RISK-FACTOR * WS-TERM-FACTOR
           COMPUTE WS-MONTHLY-PREMIUM ROUNDED =
               (WS-BASE-PREMIUM / 12) + WS-ADMIN-FEE
           MULTIPLY WS-MONTHLY-PREMIUM BY 12
               GIVING WS-ANNUAL-PREMIUM.
       4000-APPLY-DISCOUNT.
           IF POL-PAY-ANNUAL
               COMPUTE WS-DISCOUNT ROUNDED =
                   WS-ANNUAL-PREMIUM * WS-ANNUAL-DISC-RATE
               SUBTRACT WS-DISCOUNT FROM WS-ANNUAL-PREMIUM
           ELSE
               MOVE ZERO TO WS-DISCOUNT
           END-IF.
