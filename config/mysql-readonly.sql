-- Example only. Run manually as a MySQL administrator after replacing placeholders.
-- The target database must already exist. Match the host to the agent computer.
CREATE USER 'agent_reader'@'localhost' IDENTIFIED BY 'REPLACE_WITH_A_STRONG_PASSWORD';
GRANT SELECT, SHOW VIEW ON your_database.* TO 'agent_reader'@'localhost';
