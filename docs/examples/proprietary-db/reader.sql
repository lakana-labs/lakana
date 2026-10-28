-- A login for the asset that may only read the database erp. Run it as an administrator with
-- sqlcmd, which puts in the password from the environment variable READER_PASSWORD.
CREATE LOGIN lakana_reader WITH PASSWORD = '$(READER_PASSWORD)';
USE erp;
CREATE USER lakana_reader FOR LOGIN lakana_reader;
ALTER ROLE db_datareader ADD MEMBER lakana_reader;
