      *================================================================*
      * POLREC - Level Term Assurance policy record (Capita layout)    *
      *================================================================*
       01  POLICY-RECORD.
           05  POL-NUMBER             PIC X(10).
           05  POL-STATUS             PIC X.
               88  POL-ACTIVE         VALUE 'A'.
               88  POL-LAPSED         VALUE 'L'.
               88  POL-PAID-UP        VALUE 'P' 'R'.
           05  POL-SMOKER-FLAG        PIC X.
               88  POL-SMOKER         VALUE 'Y'.
           05  POL-PAY-FREQ           PIC X.
               88  POL-PAY-ANNUAL     VALUE 'A'.
               88  POL-PAY-MONTHLY    VALUE 'M'.
           05  POL-HOLDER-AGE         PIC 99.
           05  POL-TERM-YEARS         PIC 99.
           05  POL-SUM-ASSURED        PIC 9(9)V99  COMP-3.
           05  POL-BASE-RATE          PIC 9V9999.
           05  POL-FUND-VALUE         PIC S9(9)V99 COMP-3.
           05  POL-YEARS-IN-FORCE     PIC 99.
