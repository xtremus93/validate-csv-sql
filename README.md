# validate-csv-sql

Validates a local CSV file against a SQL Server table schema defined in a `CREATE TABLE` statement before import. Reports column count mismatches, column name mismatches, data type errors, and `NOT NULL` constraint violations — grouped by column with affected row numbers.

## Features

- Parses the DDL directly from a `.sql` or `.txt` file (no manual copy-paste)
- Positional column mapping (order-based, case-insensitive name check)
- Validates: `INT`, `SMALLINT`, `BIGINT`, `TINYINT`, `DECIMAL`, `NUMERIC`, `VARCHAR`, `NVARCHAR`, `DATE`, `DATETIME`, `BIT`
- Compresses error row lists into ranges for readability (e.g. `3–17, 42`)
- No external dependencies — standard library only

## Requirements

- Python 3.10 or higher

No packages need to be installed.

## Usage

```bash
python validate_csv.py <csv_file> <ddl_file>
```

### Arguments

| Argument   | Description                                            |
|------------|--------------------------------------------------------|
| `csv_file` | Path to the CSV file to validate                       |
| `ddl_file` | Path to a `.sql` or `.txt` file with the `CREATE TABLE` statement |

### Examples

```bash
# Using a .sql file exported from SSMS
python validate_csv.py employees.csv employees_schema.sql

# Using a .txt file
python validate_csv.py sales_data.csv sales_schema.txt

# Full paths
python validate_csv.py "C:\Data\employees.csv" "C:\Schemas\employees.sql"
```

### Sample output — validation passed

```
✅ Validation passed. File is ready for import.
   Rows validated: 1,500
```

### Sample output — validation failed

```
❌ Validation failed. 3 error pattern(s) found.

──────────────────────────────────────────────────────────────────────
DATA TYPE ERRORS
  [DATA TYPE] Column: Salary | Rows: 4, 19–23 | Detail: 'abc' is not a valid decimal number

──────────────────────────────────────────────────────────────────────
NULL CONSTRAINT ERRORS
  [NOT NULL] Column: EmployeeID | Rows: 7, 88 | Detail: empty/missing value in NOT NULL column

──────────────────────────────────────────────────────────────────────
Total rows inspected: 1,500
```

## DDL file format

The `.sql` or `.txt` file should contain a standard SQL Server `CREATE TABLE` statement, exactly as exported from SSMS:

```sql
CREATE TABLE dbo.Employees (
    [EmployeeID]   INT            NOT NULL,
    [FirstName]    NVARCHAR(50)   NOT NULL,
    [LastName]     VARCHAR(100)   NOT NULL,
    [Salary]       DECIMAL(10,2)  NULL,
    [HireDate]     DATE           NOT NULL,
    [IsActive]     BIT            NOT NULL,
    [DepartmentID] SMALLINT       NULL
);
```

> **Date format:** The validator expects dates in `MM/DD/YYYY` format. Edit `_DATE_FORMAT` in the script to change this.
