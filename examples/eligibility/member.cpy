      * Synthetic member eligibility record (example only, no real data).
      * Exercises every field type and mapping scenario WitnessForge supports.
       01  MEMBER-REC.
           05  REC-TYPE            PIC X(1).
               88  DETAIL-REC      VALUE 'D'.
               88  TRAILER-REC     VALUE 'T'.
           05  MEMBER-ID           PIC X(10).
           05  FIRST-NAME          PIC X(12).
           05  LAST-NAME           PIC X(15).
           05  GENDER              PIC X(1).
           05  BIRTH-DATE          PIC X(8).
           05  ZIP-CODE            PIC X(9).
           05  PHONE               PIC X(10).
           05  ACCOUNT-NO          PIC X(10).
           05  PLAN-CODE           PIC X(8).
           05  ACTIVE-FLAG         PIC X(1).
           05  PREMIUM-CENTS       PIC S9(7) COMP.
           05  COPAY-AMT           PIC S9(5)V99 COMP-3.
           05  RISK-SCORE          PIC 9(3)V9(4).
           05  HOUSEHOLD-SIZE      PIC 9(2).
           05  TERM-DATE           PIC X(8).
           05  PROGRAM-CODE        PIC X(3) OCCURS 4 TIMES.
           05  DIAG-CODE           PIC X(7) OCCURS 3 TIMES.
           05  DIAG-QUAL           PIC X(2) OCCURS 3 TIMES.
