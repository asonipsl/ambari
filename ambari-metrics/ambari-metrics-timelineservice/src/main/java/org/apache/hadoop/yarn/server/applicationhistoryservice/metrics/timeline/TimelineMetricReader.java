/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.hadoop.yarn.server.applicationhistoryservice.metrics.timeline;


import org.apache.hadoop.metrics2.sink.timeline.TimelineMetric;

import java.io.IOException;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Map;
import java.util.TreeMap;

public class TimelineMetricReader {

  private boolean ignoreInstance = false;

  public TimelineMetricReader() {}

  public TimelineMetricReader(boolean ignoreInstance) {
    this.ignoreInstance = ignoreInstance;
  }

  public TimelineMetric getTimelineMetricFromResultSet(ResultSet rs)
    throws SQLException, IOException {
    TimelineMetric metric = getTimelineMetricCommonsFromResultSet(rs);
    Map<Long, Double> sortedByTimeMetrics = new TreeMap<Long, Double>(
        PhoenixHBaseAccessor.readMetricFromJSON(rs.getString("METRICS")));
    metric.setMetricValues(sortedByTimeMetrics);
    return metric;
  }

  /**
   * Returns common part of timeline metrics record without the values.
   */
  public TimelineMetric getTimelineMetricCommonsFromResultSet(ResultSet rs)
    throws SQLException {
    TimelineMetric metric = new TimelineMetric();
    metric.setMetricName(rs.getString("METRIC_NAME"));
    metric.setAppId(rs.getString("APP_ID"));
    if (!ignoreInstance) metric.setInstanceId(rs.getString("INSTANCE_ID"));
    metric.setHostName(rs.getString("HOSTNAME"));
    metric.setTimestamp(rs.getLong("SERVER_TIME"));
    metric.setStartTime(rs.getLong("START_TIME"));
    metric.setType(rs.getString("UNITS"));
    return metric;
  }

}

