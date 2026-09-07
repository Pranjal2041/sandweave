<?php
unset($CFG);
global $CFG;
$CFG = new stdClass();
$CFG->dbtype = 'mariadb';
$CFG->dblibrary = 'native';
$CFG->dbhost = '127.0.0.1';
$CFG->dbname = 'moodle';
$CFG->dbuser = 'moodleuser';
$CFG->dbpass = 'moodlepass';
$CFG->prefix = 'mdl_';
$CFG->dboptions = ['dbpersist' => 0, 'dbport' => 3306, 'dbsocket' => '', 'dbcollation' => 'utf8mb4_unicode_ci'];
$CFG->wwwroot = 'http://localhost';
$CFG->dirroot = '/var/www/html/moodle';
$CFG->dataroot = '/var/moodledata';
$CFG->admin = 'admin';
$CFG->directorypermissions = 02770;
require_once($CFG->dirroot . '/lib/setup.php');
