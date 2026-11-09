      * Synthetic healthcare claim header record (example only, no real data)
       01  CLAIM-REC.
           05  CLAIM-ID            PIC X(10).
           05  MEMBER-ID           PIC X(12).
           05  CLAIM-TYPE          PIC X(2).
               88  INPATIENT       VALUE 'IP'.
               88  OUTPATIENT      VALUE 'OP'.
           05  SERVICE-DATE        PIC X(8).
           05  BILLED-AMT          PIC S9(7)V99 COMP-3.
           05  UNITS               PIC 9(4).
           05  DIAG-CODE           PIC X(7) OCCURS 3 TIMES.
           05  PROVIDER-NAME       PIC X(20).
