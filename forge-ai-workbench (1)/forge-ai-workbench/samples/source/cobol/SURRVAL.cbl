000100 IDENTIFICATION DIVISION.                                         SURRVAL
000200 PROGRAM-ID. SURRVAL.                                             SURRVAL
000300*    SURRENDER VALUE FOR UNIT-LINKED POLICIES                     SURRVAL
000400*    CHG1201 2009-06 MINIMUM SURRENDER VALUE ADDED                SURRVAL
000500 DATA DIVISION.                                                   SURRVAL
000600 WORKING-STORAGE SECTION.                                         SURRVAL
000700     COPY POLREC.                                                 SURRVAL
000800 01  WS-SURR-FIELDS.                                              SURRVAL
000900     05  WS-PENALTY-RATE        PIC V999.                         SURRVAL
001000     05  WS-PENALTY-AMT         PIC S9(9)V99 COMP-3.              SURRVAL
001100     05  WS-SURRENDER-VALUE     PIC S9(9)V99 COMP-3.              SURRVAL
001200     05  WS-MIN-SURRENDER       PIC 9(5)V99  VALUE 100.00.        SURRVAL
001300     05  WS-EXIT-FEE            PIC 9(3)V99  VALUE 50.00.         SURRVAL
001400 PROCEDURE DIVISION.                                              SURRVAL
001500 A000-MAIN SECTION.                                               SURRVAL
001600     IF POL-LAPSED                                                SURRVAL
001700         MOVE ZERO TO WS-SURRENDER-VALUE                          SURRVAL
001800     ELSE                                                         SURRVAL
001900         PERFORM B000-PENALTY                                     SURRVAL
002000         PERFORM C000-VALUE                                       SURRVAL
002100     END-IF                                                       SURRVAL
002200     GOBACK.                                                      SURRVAL
002300 B000-PENALTY SECTION.                                            SURRVAL
002400     EVALUATE TRUE                                                SURRVAL
002500         WHEN POL-YEARS-IN-FORCE < 3                              SURRVAL
002600             MOVE .050 TO WS-PENALTY-RATE                         SURRVAL
002700         WHEN POL-YEARS-IN-FORCE < 5                              SURRVAL
002800             MOVE .030 TO WS-PENALTY-RATE                         SURRVAL
002900         WHEN OTHER                                               SURRVAL
003000             MOVE ZERO TO WS-PENALTY-RATE                         SURRVAL
003100     END-EVALUATE.                                                SURRVAL
003200 C000-VALUE SECTION.                                              SURRVAL
003300     COMPUTE WS-PENALTY-AMT ROUNDED =                             SURRVAL
003400         POL-FUND-VALUE * WS-PENALTY-RATE                         SURRVAL
003500     COMPUTE WS-SURRENDER-VALUE =                                 SURRVAL
003600         POL-FUND-VALUE - WS-PENALTY-AMT - WS-EXIT-FEE            SURRVAL
003700     IF WS-SURRENDER-VALUE < WS-MIN-SURRENDER                     SURRVAL
003800        AND NOT POL-PAID-UP                                       SURRVAL
003900         MOVE WS-MIN-SURRENDER TO WS-SURRENDER-VALUE              SURRVAL
004000     END-IF.                                                      SURRVAL
