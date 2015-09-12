.. image:: https://api.travis-ci.org/shinken-monitoring/mod-retention-mongodb.svg?branch=master
  :target: https://travis-ci.org/shinken-monitoring/mod-retention-mongodb
.. _distributed_retention_modules:
.. _packages/distributed-retention-modules:

==================================
MongoDB retention module
==================================

The high availability allow the Arbiter to send a configuration to a spare scheduler, but a spare scheduler does not have any saved states for hosts and services. It will have to recheck them all. It's better to use a distributed retention module so spares will have all the information they need to start with an accurate picture of the current states and scheduling :)

This is why MongoDB retention module exists ...


MongoDB 
========


MongoDB is a scalable, high-performance, open source NoSQL database written in C++.

Step 1: Install MongoDB:

MongoDB installation procedure from MongoDB installation page:
  
::

   sudo apt-key adv --keyserver keyserver.ubuntu.com --recv 7F0CEB10
   echo "deb http://repo.mongodb.org/apt/debian wheezy/mongodb-org/3.0 main" | sudo tee /etc/apt/sources.list.d/mongodb-org-3.0.list
   sudo apt-get update
   sudo apt-get install -y mongodb-org
  
And we install pymongo, the python driver for MongoDB, with pip to avoid distro packages problems:
  
::

  apt-get remove python-pymongo
  pip install pymongo>=3
  
And finally we start MongoDB :
  
::

  /etc/init.d/mongodb start

  
Step 2: configure the module (if needed):
  
::

   ## Module:      retention-mongodb
   ## Loaded by:   Scheduler
   # Retention file to keep state between process restarts.
   define module {
      module_name     retention-mongodb
      module_type     mongodb_retention
      uri             mongodb://localhost
      database        shinken

      # Advanced option if you are running a cluster environnement
      #    replica_set
   }
  
Step 3: define a mongodb_retention module in your scheduler configuration file:
  
::

   define scheduler {
      scheduler_name      scheduler-master ; Just the name

      ...
      modules	retention-mongodb
      ...

   }
  
Step 4: Restart Shinken, and your Scheduler will now save its state between restarts. :)

